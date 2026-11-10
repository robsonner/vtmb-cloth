"""Controlled public-operator SMD export with observed corner provenance."""
import hashlib
import json
import os
import struct
import shutil
from contextlib import contextmanager
import uuid
from array import array
from pathlib import Path
import bpy
from . import attachment, export, pose_proof, schema
from .schema import require


def remove_staging_directory(stage, parent):
    """Verify containment on every attempt, including deferred cleanup."""
    require(stage.resolve().parent==parent and not stage.is_symlink() and stage.name.startswith('.vtmb-cloth-'),
            'Refusing cleanup outside the verified export staging parent')
    if stage.exists():
        shutil.rmtree(stage)


def defer_staging_cleanup(stage, parent):
    """Retry only Windows sharing/lock violations without blocking Blender."""
    attempts=0

    def retry():
        nonlocal attempts
        attempts+=1
        try:
            remove_staging_directory(stage,parent)
        except OSError as exc:
            if getattr(exc,'winerror',None) in (32,33) and attempts<8:
                return min(0.5*2**attempts,5.0)
            print(f'VTMB Cloth: temporary export cleanup stopped: {stage}: {exc}')
        except ValueError as exc:
            print(f'VTMB Cloth: temporary export cleanup refused: {exc}')
        return None

    bpy.app.timers.register(retry,first_interval=0.5)


@contextmanager
def staging_directory(parent, cleanup_warnings=None):
    """Inherit the chosen output parent's Windows access permissions.

    Python 3.13 TemporaryDirectory uses mkdir(0700), which can exclude the
    caller's restricted-token SID and make its own directory inaccessible.
    Ordinary mkdir inherits the already-authorized output directory's ACL.
    """
    parent=Path(parent).resolve()
    stage=parent/('.vtmb-cloth-'+uuid.uuid4().hex)
    stage.mkdir()
    try:
        yield stage
    finally:
        try:
            remove_staging_directory(stage,parent)
        except OSError as exc:
            if getattr(exc,'winerror',None) not in (32,33):
                raise
            message=f'Temporary file is locked; cleanup will retry in the background: {stage}'
            print('VTMB Cloth: '+message)
            if cleanup_warnings is not None:
                cleanup_warnings.append(message)
            defer_staging_cleanup(stage,parent)


class Capture:
    def __init__(self, document, targets):
        self.document=document
        self.before={o.as_pointer() for o in bpy.data.objects}
        self.targets={o.as_pointer() for o in targets}
        self.snapshots=[]
        self.digests=set()
        self.raw_digests=set()
        self.errors=[]
        self.busy=False
        self.bindings={b['vertex']:b['node'] for m in document['reference']['meshes'] for b in m['bindings']}

    def collect(self, scene, depsgraph):
        if self.busy:
            return
        self.busy=True
        try:
            for obj in scene.objects:
                if obj.type!='MESH' or obj.mode!='OBJECT':
                    continue
                if obj.as_pointer() in self.before and obj.as_pointer() not in self.targets:
                    continue
                mesh=obj.data
                if not all(n in mesh.attributes for n in attachment.ATTRIBUTES) or not mesh.uv_layers.active:
                    continue
                if len(mesh.polygons)!=len(self.document['reference']['triangles']):
                    continue
                # Most depsgraph events expose the same mesh repeatedly. Hash
                # bulk Blender buffers before doing per-corner decimal/weight
                # conversion; only distinct states need a full snapshot.
                h=hashlib.sha256()
                for data,field,width,kind in ((mesh.vertices,'co',3,'f'),
                                             (mesh.corner_normals,'vector',3,'f'),
                                             (mesh.uv_layers.active.data,'uv',2,'f'),
                                             (mesh.loops,'vertex_index',1,'i'),
                                             (mesh.polygons,'material_index',1,'i'),
                                             (mesh.polygons,'loop_start',1,'i'),
                                             (mesh.polygons,'loop_total',1,'i')):
                    values=array(kind,[0])*(len(data)*width)
                    data.foreach_get(field,values)
                    h.update(values.tobytes())
                for name,kind in attachment.ATTRIBUTES.items():
                    attribute=mesh.attributes[name]
                    require(attribute.domain=='CORNER','Exporter changed provenance domain')
                    width=3 if kind=='FLOAT_VECTOR' else 1
                    values=array('f' if width==3 else 'i',[0])*(len(attribute.data)*width)
                    attribute.data.foreach_get('vector' if width==3 else 'value',values)
                    h.update(values.tobytes())
                weights={vertex.index:[(obj.vertex_groups[g.group].name,g.weight) for g in vertex.groups]
                         for vertex in mesh.vertices}
                h.update(repr(weights).encode())
                h.update(repr([m.name if m else None for m in mesh.materials]).encode())
                raw_digest=h.digest()
                if raw_digest in self.raw_digests:
                    continue
                self.raw_digests.add(raw_digest)
                faces=[]
                normals=mesh.corner_normals
                uv=mesh.uv_layers.active.data
                attributes=mesh.attributes
                valid=True
                for face in mesh.polygons:
                    if len(face.loop_indices)!=3 or face.material_index>=len(mesh.materials) or mesh.materials[face.material_index] is None:
                        valid=False
                        break
                    corners=[]
                    for index in face.loop_indices:
                        source=attributes['vtmb_source_vertex'].data[index].value
                        if not 0<=source<len(self.document['reference']['vertices']):
                            valid=False
                            break
                        original=self.document['reference']['vertices'][source]['exported']
                        if original is None or attributes['vtmb_node'].data[index].value!=self.bindings.get(source,-1):
                            valid=False
                            break
                        if struct.pack('<3f',*attributes['vtmb_source_normal'].data[index].vector)!=struct.pack('<3f',*original['normal']):
                            valid=False
                            break
                        vertex=mesh.vertices[mesh.loops[index].vertex_index]
                        corner={'position':list(vertex.co),'normal':list(normals[index].vector),
                                'uv':list(uv[index].uv),'skinning':weights[vertex.index]}
                        corners.append({'source':source,'key':export.corner_key(corner,normalize_skin=True)})
                    if not valid:
                        break
                    faces.append({'material':mesh.materials[face.material_index].name,'corners':corners})
                if valid:
                    digest=hashlib.sha256(repr(faces).encode()).hexdigest()
                    if digest not in self.digests:
                        # Bound memory even if a third-party exporter generates
                        # many distinct intermediate meshes. Fail closed later.
                        require(len(self.snapshots)<32,'Too many distinct export states')
                        self.digests.add(digest)
                        self.snapshots.append(faces)
        except Exception as exc:
            if len(self.errors)<5:
                self.errors.append(str(exc))
        finally:
            self.busy=False


def export_sources(context, output_directory, reference_name, document):
    """Publish a NEW folder atomically after complete actual-file validation."""
    scene_name=context.scene.name
    settings=context.scene.vtmb_cloth
    require(settings.geometry_session is None, 'Update cloth or restore geometry positions before exporting')
    source_document = schema.loads(settings.geometry_origin.as_string()) if settings.geometry_origin else document
    objects=attachment.validate(settings,context.scene,document)
    require(len(objects)==1,'This release exports one unsplit REF object')
    obj=objects[0]
    require(context.mode=='OBJECT','Switch to Object Mode before exporting')
    modifiers=[m for m in obj.modifiers if m.type=='ARMATURE']
    require(len(modifiers)==1 and len(obj.modifiers)==1,'Unchanged export supports only the real Armature modifier')
    modifier=modifiers[0]
    require(modifier.object and modifier.use_vertex_groups and not modifier.use_bone_envelopes and not modifier.vertex_group,
            'Unsupported Armature skinning settings')
    rig=modifier.object
    require(context.scene.objects.get(rig.name)==rig,'Reference armature must belong to the current scene')
    visibility={item.name:(item.hide_viewport,item.hide_select,item.hide_get(view_layer=context.view_layer))
                for item in (obj,rig)}
    require(obj.data.shape_keys is None or all(k.value==0 for k in obj.data.shape_keys.key_blocks),
            'Set shape-key values to zero before unchanged export')
    output_directory=Path(output_directory).resolve()
    require(not output_directory.exists(),'Output folder already exists; choose a new export location/name')
    require(output_directory.parent.is_dir(),'Choose an existing parent directory')
    require(Path(reference_name).name==reference_name and reference_name.lower().endswith('.smd'), 'Invalid REF filename')
    vs=getattr(context.scene,'vs',None)
    fields=('export_path','export_format','up_axis','qc_compile')
    require(vs is not None and all(hasattr(vs,f) for f in fields),'Enable an SMD exporter exposing the Source Tools scene settings')
    operator=bpy.ops.export_scene.smd
    require('collection' in operator.get_rna_type().properties.keys(),'Installed SMD exporter lacks collection export capability')
    saved={f:getattr(vs,f) for f in fields}
    reference_path=Path(settings.source_path).with_suffix('.smd')
    require(reference_path.is_file(),'The paired original REF SMD is required for unchanged-pose proof')
    generated = settings.generated_proof is not None
    if generated:
        proof = json.loads(settings.generated_proof.as_string())
        require(proof['version'] == 1 and reference_path.resolve() == Path(proof['reference_path']).resolve(),
                'Generated reference proof no longer matches the chosen REF SMD')
        require(hashlib.sha256(reference_path.read_bytes()).hexdigest() == proof['sha256'],
                'The generation reference SMD changed; restore its original file')
        require(json.dumps(pose_proof.signature(rig), sort_keys=True) == json.dumps(proof['rest'], sort_keys=True),
                'Generated cloth reference skeleton changed; output withheld')
    selected=[o.name for o in context.selected_objects]
    active=context.view_layer.objects.active.name if context.view_layer.objects.active else None
    name='VTMB_STAGE_'+uuid.uuid4().hex
    ticket=uuid.uuid4().hex
    capture=Capture(document,objects)
    callback=capture.collect
    report=None
    cleanup_warnings=[]
    with staging_directory(output_directory.parent,cleanup_warnings) as stage:
        require(stage.parent==output_directory.parent,'Invalid export staging directory')
        raw=stage/'raw'
        raw.mkdir()
        publish=stage/'publish'
        publish.mkdir()
        try:
            original_reference=pose_proof.prove(context,modifier.object,reference_path,document,stage,source_document,
                                               verify_reference_geometry=not generated)
            # Control import changed the active/selected objects, not the REF.
            obj=attachment.validate(context.scene.vtmb_cloth,context.scene,document)[0]
            collection=bpy.data.collections.new(name)
            collection['vtmb_export_ticket']=ticket
            context.scene.collection.children.link(collection)
            collection.objects.link(obj)
            # Exporters bake the real skeleton through selection operators.
            # A hidden/unselectable rig can yield an empty bake even though its
            # modifier and bones are valid. Link both dependencies into the
            # temporary visible collection, without changing their ownership.
            rig=next(m.object for m in obj.modifiers if m.type=='ARMATURE')
            require(rig is not None,'REF lost its armature during pose proof')
            collection.objects.link(rig)
            for item in (obj,rig):
                item.hide_viewport=False
                item.hide_select=False
                item.hide_set(False,view_layer=context.view_layer)
            context.scene.vs.export_path=str(raw)
            context.scene.vs.export_format='SMD'
            context.scene.vs.up_axis='Z'
            context.scene.vs.qc_compile=False
            context.view_layer.update()
            require(operator.poll(),'SMD exporter is not ready; refresh its export list and try again')
            bpy.app.handlers.depsgraph_update_post.append(callback)
            status=operator(collection=name)
            require(status=={'FINISHED'},'SMD exporter did not finish; output withheld')
            # Exporter may restore an Undo snapshot: reacquire all RNA handles.
            scene=bpy.data.scenes[scene_name]
            attachment.validate(scene.vtmb_cloth,scene,document)
            files=list(raw.rglob('*.smd'))
            require(len(files)==1,'Exporter did not produce exactly one REF SMD')
            raw_text=files[0].read_text(encoding='utf-8-sig')
            smd_text,vcloth_text,report=export.adapt(document,raw_text,capture.snapshots,reference_name,original_reference)
            report.update(captured_states=len(capture.snapshots),capture_errors=capture.errors,
                          mode=('extended-owned-ref' if proof.get('operation') == 'append-region' else 'generated-owned-ref') if generated
                               else ('authored-owned-ref' if scene.vtmb_cloth.geometry_origin else 'unchanged-owned-ref'),normal_policy='preserved-source-corners',
                          rest_pose_proof=original_reference.get('_proof_context','REF SMD'),
                          spring_parameter_edits=json.loads(scene.vtmb_cloth.edit_log))
            (publish/reference_name).write_text(smd_text,encoding='utf-8',newline='\n')
            (publish/Path(reference_name).with_suffix('.vcloth').name).write_text(vcloth_text,encoding='utf-8',newline='\n')
            (publish/'export-report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
        finally:
            if callback in bpy.app.handlers.depsgraph_update_post:
                bpy.app.handlers.depsgraph_update_post.remove(callback)
            scene=bpy.data.scenes.get(scene_name)
            if scene:
                for field,value in saved.items():
                    setattr(scene.vs,field,value)
            collection=bpy.data.collections.get(name)
            if collection and collection.get('vtmb_export_ticket')==ticket:
                bpy.data.collections.remove(collection)
            # Object IDs can have been remapped by the exporter's Undo restore.
            for current in context.view_layer.objects:
                current.select_set(current.name in selected)
            context.view_layer.objects.active=context.view_layer.objects.get(active) if active else None
            for object_name,(hidden,unselectable,layer_hidden) in visibility.items():
                item=context.view_layer.objects.get(object_name)
                if item is not None:
                    item.hide_set(layer_hidden,view_layer=context.view_layer)
                    item.hide_viewport=hidden
                    item.hide_select=unselectable
        require(report is not None,'No validated output')
        require(not output_directory.exists(),'Output appeared during export; existing output retained')
        require(publish.resolve().parent==stage,'Invalid publish directory')
        os.rename(publish,output_directory)
    if cleanup_warnings:
        report['cleanup_warnings']=cleanup_warnings
    return report
