"""Selected-SMD rest proof through the installed public import operator."""
import uuid
import math
from pathlib import Path
import bpy
from . import export, matching, smd
from .schema import require


def signature(rig):
    return {b.name:(b.parent.name if b.parent else None,tuple(v for row in b.matrix_local for v in row),
                    b.use_deform,b.use_inherit_rotation,b.inherit_scale,b.use_local_location)
            for b in rig.data.bones}


def validate_signature(actual, expected):
    """Allow float32 import roundoff, retaining exact hierarchy and bone options.

    Source Tools' QC import can rebuild the armature for additional SMD inputs,
    introducing roundoff absent from a bare REF import. Use 64 float32 epsilons
    for the composed matrices. Translation scales with the bone's model-space
    position; rotation/scale entries have a unit floor. This is a numeric error
    budget, not permission to rebind or reposition the skeleton.
    """
    require(actual.keys()==expected.keys(), 'Reference skeleton bone names differ from selected REF SMD')
    epsilon=64*(2**-23)
    for name in sorted(expected):
        a,b=actual[name],expected[name]
        require(a[0]==b[0], f'Reference skeleton hierarchy differs on {name}')
        require(a[2:]==b[2:], f'Reference skeleton bone options differ on {name}')
        am,bm=a[1],b[1]
        require(all(math.isfinite(v) for v in (*am,*bm)),
                f'Reference skeleton has non-finite transform on {name}')
        position_scale=max(1,*(abs(m[i]) for m in (am,bm) for i in (3,7,11)))
        for i,(x,y) in enumerate(zip(am,bm)):
            scale=position_scale if i in (3,7,11) else max(1,abs(x),abs(y))
            require(abs(x-y)<=epsilon*scale,
                    f'Reference skeleton differs from selected REF SMD on {name} '
                    f'({"position" if i in (3,7,11) else "rotation/scale"}); output withheld')


def control_signature(context,path):
    original_scene=context.window.scene.name
    names=('objects','meshes','armatures','actions','collections','materials')
    controls={name:{item.as_pointer() for item in getattr(bpy.data,name)} for name in names}
    control_scene=bpy.data.scenes.new('VTMB disposable pose proof')
    try:
        context.window.scene=control_scene
        status=bpy.ops.import_scene.smd(filepath=str(path),upAxis='Z',append='NEW_ARMATURE',doAnim=False,boneMode='NONE')
        require(status=={'FINISHED'},'Control skeleton import did not finish')
        armatures=[o for o in control_scene.objects if o.type=='ARMATURE']
        require(len(armatures)==1,'Control import did not produce one skeleton')
        return signature(armatures[0])
    finally:
        if context.object and context.object.mode!='OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        context.window.scene=bpy.data.scenes[original_scene]
        bpy.data.scenes.remove(control_scene)
        for name in names:
            collection=getattr(bpy.data,name)
            for item in list(collection):
                if item.as_pointer() not in controls[name]:
                    collection.remove(item,do_unlink=True) if name=='objects' else collection.remove(item)


def prove(context, rig, reference_path, document, stage, source_document=None, verify_reference_geometry=True):
    """Prove an unedited live rig; no artist armature is replaced or changed.

    A disposable control skeleton is imported in a separate temporary scene.
    It is never linked to the artist's scene. All newly created control data is
    removed in finally. No Source Tools implementation module is imported.
    """
    source=smd.read(reference_path)
    export.validate_pose(document,source)
    # An explicitly authored mesh has a new geometry snapshot, but its source
    # skeleton must still be proved against the selected original REF SMD.
    source_document = document if source_document is None else source_document
    require(source_document['reference']['bones'] == document['reference']['bones'],
            'Geometry editing changed the reference skeleton')
    # New authoring has no original cloth geometry to match. Its caller must
    # validate the owned generated mesh separately and retain its reference
    # file hash/live rest signature. Imported VCLOTH always keeps this check.
    if verify_reference_geometry:
        matching.resolve_candidates(source_document,matching.candidates(source_document,source['faces'],matching.compatible))
    require(all(abs(rig.matrix_world[i][j]-(1 if i==j else 0))<=1e-7 for i in range(4) for j in range(4)),
            'Reference armature must have unchanged model-space transforms')
    require(not rig.constraints and all(not b.constraints for b in rig.pose.bones),
            'Unchanged export does not support added armature constraints')
    before=signature(rig)
    lines=['version 1','nodes']
    for i,b in source['bones'].items():
        require('"' not in b['name'] and '\n' not in b['name'],'Invalid reference bone name')
        lines.append(f'{i} "{b["name"]}" {b["parent"]}')
    lines+=['end','skeleton','time 0']
    for i,values in source['pose'].items():
        lines.append(str(i)+' '+' '.join(format(v,'.17g') for v in values))
    lines+=['end','']
    path=Path(stage)/('pose-proof-'+uuid.uuid4().hex+'.smd')
    path.write_text('\n'.join(lines),encoding='utf-8')
    validate_signature(before,control_signature(context,path))
    source['_proof_context']='REF SMD'
    require(signature(rig)==before,'Reference skeleton changed during pose proof')
    return source
