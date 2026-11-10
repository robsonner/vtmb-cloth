"""VTMB Cloth file inspector; independent of Blender Source Tools."""
bl_info = {
    'name': 'VTMB Cloth', 'author': 'VTMB SDK contributors',
    'version': (0, 10, 3), 'blender': (5, 0, 0),
    'location': 'View3D > Sidebar > VTMB Cloth',
    'description': 'Inspect and edit VTMB cloth on the original REF mesh',
    'category': 'Import-Export',
}

import hashlib
import bpy
from bpy.props import BoolProperty, IntProperty, FloatProperty, PointerProperty, StringProperty, CollectionProperty, EnumProperty
from bpy_extras.io_utils import ImportHelper, ExportHelper
from bpy.app.handlers import persistent
from . import schema, smd, matching, scene_data, attachment, editing, export, pose_proof, export_scene, geometry, roles, generation, regions, workflow_ui

_cache = {}
_binding_counts = {}
_gpu_cache = {}
_draw_handle = None


def reload_dependencies():
    """Refresh pure/helper modules when Blender re-enables an upgraded ZIP.

    Blender may reload __init__ while keeping old package children in
    sys.modules. None of these children register Blender classes or callbacks.
    Reload in dependency order, without changing scene data or the payload.
    """
    import importlib
    importlib.invalidate_caches()
    for module in (schema, smd, matching, scene_data, attachment, editing, export, pose_proof, export_scene, geometry, roles, generation, regions, workflow_ui):
        importlib.reload(module)


@persistent
def invalidate_cache(*args):
    _cache.clear()
    _binding_counts.clear()
    _gpu_cache.clear()


def document(settings):
    text = settings.payload
    if text is None:
        raise ValueError('Import a VCLOTH file first')
    # The canonical payload is an imported snapshot. Never copy/hash the entire
    # Text datablock during viewport or panel redraw. Manual Text edits require
    # Refresh; load/undo/redo and import/unload invalidate all derived state.
    key = text.as_pointer()
    if key not in _cache:
        try:
            d = schema.loads(text.as_string())
            _cache[key] = (d, schema.validate(d))
            counts = {}
            for mesh in d['reference']['meshes']:
                for binding in mesh['bindings']:
                    node = binding['node']
                    counts[node] = counts.get(node, 0) + 1
            _binding_counts[key] = counts
        except ValueError as exc:
            # Invalid edited payloads must not be parsed again on every frame.
            _cache[key] = exc
    if isinstance(_cache[key], ValueError):
        raise _cache[key]
    return _cache[key]


def redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def changed(self, context):
    redraw()


class VTMBTarget(bpy.types.PropertyGroup):
    object: PointerProperty(type=bpy.types.Object)


class VTMBSettings(bpy.types.PropertyGroup):
    workflow: EnumProperty(name='Task', items=(
        ('EDIT', 'Edit Existing Cloth', 'Change shape, attachment and parameters'),
        ('APPEND', 'Add Cloth Region', 'Keep existing cloth and add a new disconnected region'),
        ('NEW', 'Create New Cloth', 'Create a setup without retaining existing simulation')),
        default='EDIT', update=changed)
    ui_display: BoolProperty(default=False)
    ui_advanced: BoolProperty(default=False)
    captured_face_count: IntProperty(min=0)
    captured_anchor_count: IntProperty(min=0)
    captured_object: StringProperty()
    payload: PointerProperty(type=bpy.types.Text)
    source_path: StringProperty(name='Source path')
    source_hash: StringProperty(name='Source SHA-256')
    show_overlay: BoolProperty(name='Reference-space overlay', default=False, update=changed)
    show_springs: BoolProperty(name='Springs', default=True, update=changed)
    show_bindings: BoolProperty(name='Node-to-render references', default=False, update=changed)
    node: IntProperty(name='Node ID', min=0, update=changed)
    spring: IntProperty(name='Spring ID', min=0, update=changed)
    edit_log: StringProperty(default='[]')
    geometry_session: PointerProperty(type=bpy.types.Text)
    geometry_origin: PointerProperty(type=bpy.types.Text)
    generated_proof: PointerProperty(type=bpy.types.Text)
    authoring_draft: PointerProperty(type=bpy.types.Text)
    authoring_anchors: StringProperty(default='[]')
    authoring_mode: EnumProperty(items=(('NEW', 'New Cloth', ''), ('APPEND', 'Additional Region', '')), default='NEW')
    targets: CollectionProperty(type=VTMBTarget)
    binding_token: StringProperty()
    binding_digest: StringProperty()
    binding_status: StringProperty(default='No REF attachment')


class VTMBImport(bpy.types.Operator, ImportHelper):
    bl_idname = 'vtmb_cloth.import_file'
    bl_label = 'Import VCLOTH for Inspection'
    bl_options = {'REGISTER', 'UNDO'}
    filename_ext = '.vcloth'
    filter_glob: StringProperty(default='*.vcloth', options={'HIDDEN'})

    def execute(self, context):
        try:
            d = schema.read(self.filepath)
            # Validate before any scene mutation. Retain every JSON field.
            raw = schema.dumps(d)
            from pathlib import Path
            digest = hashlib.sha256(Path(self.filepath).read_bytes()).hexdigest()
        except (ValueError, OSError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        settings = context.scene.vtmb_cloth
        attachment.detach(settings)
        old = settings.payload
        text = bpy.data.texts.new('VTMB Cloth: ' + Path(self.filepath).name)
        text.write(raw)
        settings.payload = text
        settings.source_path = self.filepath
        settings.source_hash = digest
        settings.node = settings.spring = 0
        settings.edit_log = '[]'
        settings.authoring_draft = None
        settings.authoring_anchors = '[]'
        settings.authoring_mode = 'NEW'
        settings.show_overlay = False
        if settings.workflow != 'APPEND':
            settings.workflow = 'EDIT'
        if old is not None and old.users == 0 and old.name.startswith('VTMB Cloth: '):
            bpy.data.texts.remove(old)
        invalidate_cache()
        document(settings)
        self.report({'INFO'}, 'VCLOTH loaded. Attach the unchanged REF to enable source export.')
        redraw()
        return {'FINISHED'}


class VTMBClear(bpy.types.Operator):
    bl_idname = 'vtmb_cloth.clear'
    bl_label = 'Unload Cloth Data'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.vtmb_cloth
        attachment.detach(s)
        old = s.payload
        s.payload = None
        s.show_overlay = False
        s.source_path = s.source_hash = ''
        s.edit_log = '[]'
        s.authoring_draft = None
        s.authoring_anchors = '[]'
        s.authoring_mode = 'NEW'
        if old is not None and old.users == 0 and old.name.startswith('VTMB Cloth: '):
            bpy.data.texts.remove(old)
        invalidate_cache()
        redraw()
        return {'FINISHED'}


class VTMBRefresh(bpy.types.Operator):
    bl_idname = 'vtmb_cloth.refresh'
    bl_label = 'Refresh Edited Payload'
    bl_description = 'Re-read and validate the cached Text payload after manual edits'

    def execute(self, context):
        invalidate_cache()
        try:
            settings = context.scene.vtmb_cloth
            document(settings)
            if settings.targets:
                settings.binding_status = 'Payload refreshed; validate attachment again'
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            redraw()
            return {'CANCELLED'}
        redraw()
        return {'FINISHED'}


class VTMBAttach(bpy.types.Operator):
    bl_idname = 'vtmb_cloth.attach'
    bl_label = 'Attach to Selected REF Objects'
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        self.targets = tuple(context.selected_objects)
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        self.layout.label(text='Confirm the complete REF set (exclude capsules/physics):')
        for obj in self.targets:
            self.layout.label(text=obj.name)

    def execute(self, context):
        targets = getattr(self, 'targets', tuple(context.selected_objects))
        try:
            settings = context.scene.vtmb_cloth
            d, stats = document(settings)
            count = attachment.attach(settings, context.scene, d, targets)
            self.report({'INFO'}, f'Attached {count} source faces. Export revalidates before publishing.')
        except (ValueError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class VTMBDetach(bpy.types.Operator):
    bl_idname = 'vtmb_cloth.detach'
    bl_label = 'Detach REF Mapping'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        attachment.detach(context.scene.vtmb_cloth)
        redraw()
        return {'FINISHED'}


class VTMBValidate(bpy.types.Operator):
    bl_idname = 'vtmb_cloth.validate_attachment'
    bl_label = 'Validate REF Attachment'

    def execute(self, context):
        settings = context.scene.vtmb_cloth
        try:
            d, _ = document(settings)
            attachment.validate(settings, context.scene, d)
        except (ValueError, RuntimeError) as exc:
            settings.binding_status = 'Attachment invalid; see validation error'
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        settings.binding_status = 'Last check passed; unchanged source export available'
        self.report({'INFO'}, 'REF and preserved corner identities remain unchanged')
        return {'FINISHED'}


class VTMBSelect(bpy.types.Operator):
    bl_idname = 'vtmb_cloth.select_bound'
    bl_label = 'Select Associated Mesh Vertices'
    bl_options = {'REGISTER', 'UNDO'}
    role: EnumProperty(name='Selection', items=(('ACTIVE', 'Active Node', ''),
                     ('FIXED', 'Fixed Bindings', ''), ('FREE', 'Free Bindings', ''),
                     ('UNBOUND', 'Unbound Geometry', '')))

    def execute(self, context):
        settings = context.scene.vtmb_cloth
        try:
            d, _ = document(settings)
            objects = attachment.validate(settings, context.scene, d)
            schema.require(context.mode == 'OBJECT', 'Switch to Object Mode before selecting REF vertices')
            for obj in context.selected_objects:
                obj.select_set(False)
            for obj in objects:
                obj.select_set(True)
            context.view_layer.objects.active = objects[0]
            nodes = d['simulation']['nodes']
            selected = 0
            for obj in objects:
                mesh = obj.data
                for vertex in mesh.vertices:
                    vertex.select = False
                for edge in mesh.edges:
                    edge.select = False
                for polygon in mesh.polygons:
                    polygon.select = False
                for loop in mesh.loops:
                    node = mesh.attributes['vtmb_node'].data[loop.index].value
                    choose = ((self.role == 'ACTIVE' and node == settings.node) or
                              (self.role == 'UNBOUND' and node == -1) or
                              (node >= 0 and self.role == ('FIXED' if nodes[node]['fixed'] else 'FREE')))
                    if choose:
                        mesh.vertices[loop.vertex_index].select = True
                selected += sum(v.select for v in mesh.vertices)
                mesh.update()
            self.report({'INFO'}, f'{selected} mesh vertices selected; shared corners select their common vertex')
        except (ValueError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        redraw()
        return {'FINISHED'}


class VTMBEditSpring(bpy.types.Operator):
    bl_idname = 'vtmb_cloth.edit_spring'
    bl_label = 'Edit Selected Spring Factors'
    bl_options = {'REGISTER','UNDO'}
    spring_id: IntProperty(name='Spring ID',min=0,options={'HIDDEN'})
    weight_a: FloatProperty(name='Endpoint A factor (>= 0)',min=0,precision=6)
    weight_b: FloatProperty(name='Endpoint B factor (<= 0)',max=0,precision=6)
    revision: StringProperty(options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        s = context.scene.vtmb_cloth
        return context.mode == 'OBJECT' and bool(s.targets) and s.geometry_session is None

    def invoke(self, context, event):
        try:
            d,_=document(context.scene.vtmb_cloth)
            self.spring_id=context.scene.vtmb_cloth.spring
            schema.integer(self.spring_id,len(d['simulation']['springs'])-1)
            spring=d['simulation']['springs'][self.spring_id]
            self.weight_a=spring['weight_a']
            self.weight_b=spring['weight_b']
            self.revision=hashlib.sha256(schema.dumps(d).encode()).hexdigest()
            self.lock_a=spring['weight_a']==0
            self.lock_b=spring['weight_b']==0
        except (ValueError,RuntimeError) as exc:
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self,width=460)

    def draw(self, context):
        self.layout.label(text=f'Spring {self.spring_id}: experimental raw endpoint factors')
        self.layout.label(text='Physical effect is not established as a stiffness control.')
        row=self.layout.row()
        row.enabled=not getattr(self,'lock_a',False)
        row.prop(self,'weight_a')
        row=self.layout.row()
        row.enabled=not getattr(self,'lock_b',False)
        row.prop(self,'weight_b')
        self.layout.label(text='Zero factors, rest length, endpoints and tables stay unchanged.')

    def execute(self, context):
        import json
        try:
            settings=context.scene.vtmb_cloth
            d,_=document(settings)
            schema.require(self.properties.is_property_set('weight_a') and self.properties.is_property_set('weight_b'),
                           'Both endpoint factors must be supplied explicitly')
            if self.revision:
                schema.require(self.revision==hashlib.sha256(schema.dumps(d).encode()).hexdigest(),'Cloth data changed while the dialog was open; reopen it')
            objects=attachment.validate(settings,context.scene,d)
            updated,entry=editing.edit_spring(d,self.spring_id,self.weight_a,self.weight_b)
            if not entry['changes']:
                self.report({'INFO'},'Spring factors are unchanged')
                return {'FINISHED'}
            faces,_=scene_data.read_faces(objects)
            new_digest=attachment.fingerprint(updated,faces)
            raw=schema.dumps(updated)
            history=json.loads(settings.edit_log)
            schema.require(isinstance(history,list),'Invalid spring edit history')
            history.append(entry)
            history_text=json.dumps(history,allow_nan=False)
            # Immutable Text snapshots let global Undo restore payload and its
            # ownership digest together, without relying on Text Editor undo.
            payload=bpy.data.texts.new('VTMB Cloth: spring edit')
            payload.write(raw)
            settings.payload=payload
            settings.binding_digest=new_digest
            settings.edit_log=history_text
            settings.binding_status=f'Spring {self.spring_id} factors edited; REF unchanged'
            invalidate_cache()
            document(settings)
        except (ValueError,RuntimeError) as exc:
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}
        self.report({'INFO'},f'Updated spring {self.spring_id}. Export REF + VCLOTH to test in game.')
        redraw()
        return {'FINISHED'}


class VTMBEditSpringBatch(bpy.types.Operator):
    bl_idname = 'vtmb_cloth.edit_spring_batch'
    bl_label = 'Bulk Edit from Selected Vertices'
    bl_options = {'REGISTER','UNDO'}
    multiplier: FloatProperty(name='Raw factor multiplier',default=0.8,min=0.000001,precision=6)
    endpoint: EnumProperty(name='Factors',items=(('BOTH','A and B','Scale both raw factors'),
        ('A','A only','Keep B unchanged'),('B','B only','Keep A unchanged')),default='BOTH')
    rule: EnumProperty(name='Include springs',items=(
        ('ANY','At least one selected endpoint','Include springs crossing the selection boundary'),
        ('BOTH','Both endpoints selected','Include only springs inside the selection')),default='ANY')
    selected_nodes: StringProperty(default='[]',options={'HIDDEN'})
    revision: StringProperty(options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        s = context.scene.vtmb_cloth
        return context.mode in {'OBJECT', 'EDIT_MESH'} and bool(s.targets) and s.geometry_session is None

    @workflow_ui.selection_action
    def invoke(self, context, event):
        import json
        try:
            settings=context.scene.vtmb_cloth
            d,_=document(settings)
            objects=attachment.validate(settings,context.scene,d)
            nodes=set()
            for obj in objects:
                if not obj.select_get():
                    continue
                mesh=obj.data
                data=mesh.attributes['vtmb_node'].data
                for loop in mesh.loops:
                    if mesh.vertices[loop.vertex_index].select and data[loop.index].value>=0:
                        nodes.add(data[loop.index].value)
            schema.require(nodes,'Select cloth vertices in Edit Mode')
            self.selected_nodes=json.dumps(sorted(nodes))
            self.revision=hashlib.sha256(schema.dumps(d).encode()).hexdigest()
        except (ValueError,RuntimeError) as exc:
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self,width=510)

    def draw(self, context):
        import json
        d,_=document(context.scene.vtmb_cloth)
        ids=editing.selected_springs(d,json.loads(self.selected_nodes),self.rule)
        self.layout.label(text=f'{len(ids)} springs in this batch')
        self.layout.prop(self,'rule')
        self.layout.prop(self,'endpoint')
        self.layout.prop(self,'multiplier')
        self.layout.label(text='0.8 = reduce current factors by 20%; 1.0 = no change')
        self.layout.label(text='Raw factors, not verified stiffness. Zero factors stay zero.')

    @workflow_ui.selection_action
    def execute(self, context):
        import json
        try:
            settings=context.scene.vtmb_cloth
            d,_=document(settings)
            schema.require(bool(self.revision) and self.revision==hashlib.sha256(schema.dumps(d).encode()).hexdigest(),
                           'Cloth data changed or selection is missing; reopen the bulk editor')
            objects=attachment.validate(settings,context.scene,d)
            ids=editing.selected_springs(d,json.loads(self.selected_nodes),self.rule)
            updated,entries=editing.scale_springs(d,ids,self.multiplier,self.endpoint)
            if not entries:
                self.report({'INFO'},'Selected spring factors are unchanged')
                return {'FINISHED'}
            faces,_=scene_data.read_faces(objects)
            new_digest=attachment.fingerprint(updated,faces)
            raw=schema.dumps(updated)
            history=json.loads(settings.edit_log)
            schema.require(isinstance(history,list),'Invalid spring edit history')
            history.extend(entries)
            history_text=json.dumps(history,allow_nan=False)
            payload=bpy.data.texts.new('VTMB Cloth: bulk spring edit')
            payload.write(raw)
            settings.payload=payload
            settings.binding_digest=new_digest
            settings.edit_log=history_text
            settings.binding_status=f'{len(entries)} springs edited; REF unchanged'
            invalidate_cache()
            document(settings)
        except (ValueError,RuntimeError) as exc:
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}
        self.report({'INFO'},f'Updated {len(entries)} springs. Export REF + VCLOTH to test in game.')
        redraw()
        return {'FINISHED'}


class VTMBCaptureCloth(bpy.types.Operator):
    bl_idname = 'vtmb_cloth.capture_surface'
    bl_label = 'Capture Cloth Faces'
    bl_description = 'Capture selected triangle faces on an unowned REF before creating a new cloth setup'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        s = context.scene.vtmb_cloth
        return context.mode in {'OBJECT', 'EDIT_MESH'} and context.object is not None and context.object.type == 'MESH' and not s.targets and s.payload is None

    @workflow_ui.selection_action
    def execute(self, context):
        try:
            s = context.scene.vtmb_cloth
            raw = generation.capture(s, context.object)
            text = bpy.data.texts.new('VTMB Cloth: selected surface')
            text.write(raw)
            s.authoring_draft = text
            s.authoring_anchors = '[]'
            captured = geometry.decode_snapshot(text)
            s.captured_face_count = len(captured['faces'])
            s.captured_anchor_count = 0
            s.captured_object = context.object.name
            s.authoring_mode = 'NEW'
            s.workflow = 'NEW'
        except (ValueError, RuntimeError, KeyError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, 'Surface captured. Select attachment vertices in Edit Mode, then Set Attachment Points.')
        redraw()
        return {'FINISHED'}


class VTMBCaptureAdditionalCloth(bpy.types.Operator):
    bl_idname = 'vtmb_cloth.capture_additional_surface'
    bl_label = 'Capture Additional Cloth Faces'
    bl_description = 'Preserve the loaded base cloth and capture only the newly added disconnected geometry'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        s = context.scene.vtmb_cloth
        return (context.mode in {'OBJECT', 'EDIT_MESH'} and context.object is not None and context.object.type == 'MESH'
                and s.payload is not None and not s.targets and s.geometry_session is None)

    @workflow_ui.selection_action
    def execute(self, context):
        try:
            s = context.scene.vtmb_cloth
            # Explicitly refresh the source payload; a cached redraw document
            # must not silently hide a manual Text change during authoring.
            d = schema.loads(s.payload.as_string())
            raw = regions.capture(s, context.object, d)
            text = bpy.data.texts.new('VTMB Cloth: additional surface')
            text.write(raw)
            s.authoring_draft = text
            s.authoring_anchors = '[]'
            captured = geometry.decode_snapshot(text)
            s.captured_face_count = len(captured['faces'])
            s.captured_anchor_count = 0
            s.captured_object = context.object.name
            s.authoring_mode = 'APPEND'
            s.workflow = 'APPEND'
        except (ValueError, RuntimeError, KeyError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, 'Existing cloth matched. Select new-region attachment vertices, then Set Attachment Points.')
        redraw()
        return {'FINISHED'}


class VTMBAddClothRegion(bpy.types.Operator):
    bl_idname = 'vtmb_cloth.add_region'
    bl_label = 'Add Cloth Region'
    bl_description = 'Append the captured region while preserving existing cloth records and independent spring networks'
    bl_options = {'REGISTER', 'UNDO'}
    strength: FloatProperty(name='New region raw spring strength', default=2.0, min=0.000001)
    bend_ratio: FloatProperty(name='New region bending squared rest ratio', default=0.7, min=0.000001, max=1.0)
    shared_time: FloatProperty(options={'HIDDEN'})
    revision: StringProperty(options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        s = context.scene.vtmb_cloth
        return (context.mode == 'OBJECT' and s.payload is not None and not s.targets
                and s.authoring_draft is not None and s.authoring_mode == 'APPEND' and s.authoring_anchors != '[]')

    def invoke(self, context, event):
        try:
            s = context.scene.vtmb_cloth
            generation.draft(s, context.scene)
            self.shared_time = schema.loads(s.payload.as_string())['simulation']['time_step_scale']
            self.revision = hashlib.sha256(s.authoring_draft.as_string().encode()).hexdigest()
        except (ValueError, RuntimeError, KeyError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=540)

    def draw(self, context):
        self.layout.label(text='Existing cloth factors, rest lengths and batches are preserved.')
        self.layout.label(text='Unrelated rigid geometry uses the current REF.')
        self.layout.label(text='No springs are created between the existing and added regions.')
        self.layout.label(text=f'Shared time-step scale remains {self.shared_time:.9g}')
        self.layout.prop(self, 'strength')
        self.layout.prop(self, 'bend_ratio')

    def execute(self, context):
        try:
            s = context.scene.vtmb_cloth
            schema.require(s.authoring_draft is not None and self.revision and
                           self.revision == hashlib.sha256(s.authoring_draft.as_string().encode()).hexdigest(),
                           'Additional region changed; reopen Add Cloth Region')
            entry = regions.add(context, s, self.strength, self.bend_ratio)
            context.scene.vtmb_cloth.workflow = 'EDIT'
            invalidate_cache()
        except (ValueError, RuntimeError, KeyError, TypeError, OverflowError, OSError) as exc:
            invalidate_cache()
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, f'Added {entry["added_nodes"]} nodes; preserved {entry["base_nodes"]} existing nodes. Export REF + VCLOTH.')
        redraw()
        return {'FINISHED'}


class VTMBGeneratorAnchors(bpy.types.Operator):
    bl_idname = 'vtmb_cloth.generator_anchors'
    bl_label = 'Set Attachment Points'
    bl_description = 'Capture selected cloth vertices as fixed anchors, including every rigid/cloth boundary point'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        s = context.scene.vtmb_cloth
        return (context.mode in {'OBJECT', 'EDIT_MESH'} and s.authoring_draft is not None and not s.targets
                and ((s.authoring_mode == 'NEW' and s.payload is None)
                     or (s.authoring_mode == 'APPEND' and s.payload is not None)))

    @workflow_ui.selection_action
    def execute(self, context):
        import json
        try:
            s = context.scene.vtmb_cloth
            fixed = generation.anchors(s, context.scene)
            s.authoring_anchors = json.dumps(fixed)
            s.captured_anchor_count = len(fixed)
        except (ValueError, RuntimeError, KeyError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        next_step = 'Add Cloth Region' if s.authoring_mode == 'APPEND' else 'Generate Cloth and choose the bind-pose REF SMD'
        self.report({'INFO'}, f'{len(fixed)} fixed anchor vertices recorded. Choose {next_step}.')
        redraw()
        return {'FINISHED'}


class VTMBGenerateCloth(bpy.types.Operator, ImportHelper):
    bl_idname = 'vtmb_cloth.generate'
    bl_label = 'Generate Cloth — Choose Bind-pose REF SMD'
    bl_description = 'Generate a new setup using captured faces/anchors and the real skeleton from a bind-pose REF SMD'
    bl_options = {'REGISTER', 'UNDO'}
    filename_ext = '.smd'
    filter_glob: StringProperty(default='*.smd', options={'HIDDEN'})
    strength: FloatProperty(name='Raw spring total strength', default=2.0, min=0.000001)
    bend_ratio: FloatProperty(name='Bending squared rest ratio', default=0.7, min=0.000001, max=1.0)
    time_scale: FloatProperty(name='Raw time-step scale', default=3.0, min=0.000001)

    @classmethod
    def poll(cls, context):
        s = context.scene.vtmb_cloth
        return context.mode == 'OBJECT' and s.authoring_draft is not None and s.payload is None and s.authoring_anchors != '[]'

    def execute(self, context):
        try:
            d = generation.generate(context, context.scene.vtmb_cloth, self.filepath,
                                    self.strength, self.bend_ratio, self.time_scale)
            context.scene.vtmb_cloth.workflow = 'EDIT'
            invalidate_cache()
        except (ValueError, RuntimeError, KeyError, TypeError, OverflowError, OSError) as exc:
            invalidate_cache()
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, f'Generated {len(d["simulation"]["nodes"])} cloth nodes. Export REF + VCLOTH to a new folder.')
        redraw()
        return {'FINISHED'}


class VTMBSetNodeRole(bpy.types.Operator):
    bl_idname = 'vtmb_cloth.set_node_role'
    bl_label = 'Set Selected Cloth Node Roles'
    bl_description = 'Set selected render/reference nodes fixed or free and rebuild dependent simulation data'
    bl_options = {'REGISTER', 'UNDO'}
    role: EnumProperty(name='Role', items=(
        ('FIXED', 'Fixed', 'Follow original reference skinning'),
        ('FREE', 'Free', 'Participate in cloth simulation')), default='FIXED')
    activation_strength: FloatProperty(name='Zero-strength spring activation', default=2.0, min=0.000001,
        description='Total raw correction magnitude only when activating a spring with no retained nonzero strength')
    selected_nodes: StringProperty(default='[]', options={'HIDDEN'})
    revision: StringProperty(options={'HIDDEN'})
    selected_count: IntProperty(options={'HIDDEN'})
    changed_count: IntProperty(options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        s = context.scene.vtmb_cloth
        return context.mode in {'OBJECT', 'EDIT_MESH'} and bool(s.targets) and s.geometry_session is None

    @workflow_ui.selection_action
    def invoke(self, context, event):
        import json
        try:
            s = context.scene.vtmb_cloth
            d, _ = document(s)
            objects, _ = attachment.validate_owned(s, context.scene, d)
            nodes = roles.selected_nodes(d, objects)
            self.selected_nodes = json.dumps(nodes)
            self.revision = hashlib.sha256(schema.dumps(d).encode()).hexdigest()
            self.selected_count = len(nodes)
            self.changed_count = sum(d['simulation']['nodes'][i]['fixed'] != (self.role == 'FIXED') for i in nodes)
        except (ValueError, RuntimeError, KeyError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=570)

    def draw(self, context):
        self.layout.label(text=f'{self.selected_count} selected nodes; target: {self.role}')
        self.layout.label(text=f'{self.changed_count} nodes will change role')
        self.layout.label(text='Shared render aliases and reference vertices select their complete nodes.')
        self.layout.label(text='All spring factors are redistributed by structural distance to fixed nodes.')
        self.layout.label(text='Existing total strength and rest lengths are retained; collision data is rebuilt.')
        self.layout.prop(self, 'activation_strength')
        self.layout.label(text='Experimental authoring policy; not a verified stiffness control.')

    @workflow_ui.selection_action
    def execute(self, context):
        import json
        try:
            s = context.scene.vtmb_cloth
            d, _ = document(s)
            schema.require(self.revision and self.revision == hashlib.sha256(schema.dumps(d).encode()).hexdigest(),
                           'Cloth data changed or selection is missing; reopen Set Fixed/Free')
            entry = roles.apply(s, context.scene, d, json.loads(self.selected_nodes),
                                self.role == 'FIXED', self.activation_strength)
            invalidate_cache()
            if entry is None:
                self.report({'INFO'}, f'Selected nodes are already {self.role}')
            else:
                self.report({'INFO'}, f'{len(entry["changed_old_nodes"])} nodes set {self.role}. Export REF + VCLOTH.')
                if entry['activated_zero_strength_springs']:
                    self.report({'WARNING'}, f'{len(entry["activated_zero_strength_springs"])} zero-strength springs activated with the dialog value')
        except (ValueError, RuntimeError, KeyError, TypeError, OverflowError) as exc:
            invalidate_cache()
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        redraw()
        return {'FINISHED'}


class VTMBGeometryBegin(bpy.types.Operator):
    bl_idname = 'vtmb_cloth.geometry_begin'
    bl_label = 'Begin Geometry Edit'
    bl_description = 'Capture the validated REF before moving existing vertices in Edit Mode'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        s = context.scene.vtmb_cloth
        return context.mode == 'OBJECT' and bool(s.targets) and s.geometry_session is None

    def execute(self, context):
        try:
            settings = context.scene.vtmb_cloth
            d, _ = document(settings)
            raw = geometry.begin(settings, context.scene, d)
            snapshot = bpy.data.texts.new('VTMB Cloth: geometry baseline')
            snapshot.write(raw)
            if settings.geometry_origin is None:
                settings.geometry_origin = settings.payload
            settings.geometry_session = snapshot
            settings.binding_status = 'Geometry edit active: move existing vertices, then update cloth'
        except (ValueError, RuntimeError, KeyError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, 'Edit existing vertices on Basis, then return to Object Mode and update cloth.')
        redraw()
        return {'FINISHED'}


class VTMBGeometryUpdate(bpy.types.Operator):
    bl_idname = 'vtmb_cloth.geometry_update'
    bl_label = 'Update Cloth from Edited Mesh'
    bl_description = 'Validate position-only edits and update nodes, rest lengths, normals and tangent coefficients'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and context.scene.vtmb_cloth.geometry_session is not None

    def execute(self, context):
        try:
            settings = context.scene.vtmb_cloth
            d, _ = document(settings)
            entry = geometry.apply(settings, context.scene, d)
            invalidate_cache()
        except (ValueError, RuntimeError, KeyError, TypeError, OverflowError) as exc:
            invalidate_cache()
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, f'Updated {entry["vertices"]} source vertices and {entry["nodes"]} cloth nodes. Export REF + VCLOTH.')
        redraw()
        return {'FINISHED'}


class VTMBGeometryRestore(bpy.types.Operator):
    bl_idname = 'vtmb_cloth.geometry_restore'
    bl_label = 'Restore Geometry Edit Positions'
    bl_description = 'Restore captured base and shape-key positions; does not repair topology, UV or weight edits'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and context.scene.vtmb_cloth.geometry_session is not None

    def execute(self, context):
        import json
        backup = None
        try:
            s = context.scene.vtmb_cloth
            baseline = geometry.decode_snapshot(s.geometry_session)
            objects = attachment.targets(s, context.scene)
            schema.require(len(objects) == 1 and objects[0].name == baseline['object'], 'Geometry target changed')
            obj = objects[0]
            schema.require(geometry.canonical(geometry.mesh_state(obj)) == geometry.canonical(baseline['state']),
                           'Topology, provenance or rig changed; use Blender Undo to restore it')
            backup = obj.data.copy()
            for vertex, position in zip(obj.data.vertices, baseline['positions']):
                vertex.co = position
            if obj.data.shape_keys:
                for key, positions in zip(obj.data.shape_keys.key_blocks, geometry.unpack_keys(baseline['keys'])):
                    for point, position in zip(key.data, positions):
                        point.co = position
            obj.data.update()
            d, _ = document(s)
            attachment.validate_owned(s, context.scene, d)
            s.geometry_session = None
            s.binding_status = 'Geometry positions restored; attachment validated'
        except (ValueError, RuntimeError, KeyError) as exc:
            if backup is not None:
                current = obj.data
                obj.data = backup
                if current.users == 0:
                    bpy.data.meshes.remove(current)
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        bpy.data.meshes.remove(backup)
        invalidate_cache()
        redraw()
        return {'FINISHED'}


class VTMBCancelAuthoring(bpy.types.Operator):
    bl_idname = 'vtmb_cloth.cancel_authoring'
    bl_label = 'Cancel Creation'
    bl_description = 'Discard captured surface and attachment selections; keep mesh and loaded cloth data'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene.vtmb_cloth.authoring_draft is not None

    def execute(self, context):
        s = context.scene.vtmb_cloth
        s.authoring_draft = None
        s.authoring_anchors = '[]'
        s.captured_face_count = s.captured_anchor_count = 0
        s.captured_object = ''
        self.report({'INFO'}, 'Captured selections discarded. Mesh and loaded cloth data are unchanged.')
        redraw()
        return {'FINISHED'}


class VTMBFinishSelection(bpy.types.Operator):
    bl_idname = 'vtmb_cloth.finish_selection'
    bl_label = 'Finish Selection'
    bl_description = 'Return to Object Mode for generation, shape validation or export'

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def execute(self, context):
        bpy.ops.object.mode_set(mode='OBJECT')
        redraw()
        return {'FINISHED'}


class VTMBPanel(bpy.types.Panel):
    bl_label = 'VTMB Cloth'
    bl_idname = 'VTMB_PT_cloth'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'VTMB Cloth'

    def draw(self, context):
        import sys
        workflow_ui.draw_panel(self.layout, context, sys.modules[__name__])


def overlay_batches(settings, d):
    """Upload static buffers once per payload/window, not once per frame."""
    import gpu
    from gpu_extras.batch import batch_for_shader
    window = bpy.context.window
    key = (settings.payload.as_pointer(), window.as_pointer() if window else 0)
    if key not in _gpu_cache:
        nodes = d['simulation']['nodes']
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')

        def batch(kind, positions):
            return batch_for_shader(shader, kind, {'pos': positions}) if positions else None

        _gpu_cache[key] = {
            'shader': shader,
            'springs': batch('LINES', [nodes[i]['position'] for spring in d['simulation']['springs'] for i in spring['nodes']]),
            'bindings': batch('LINES', [p for m in d['reference']['meshes'] for b in m['bindings']
                                      for p in (nodes[b['node']]['position'], d['reference']['vertices'][b['vertex']]['source_position'])]),
            'fixed': batch('POINTS', [n['position'] for n in nodes if n['fixed']]),
            'free': batch('POINTS', [n['position'] for n in nodes if not n['fixed']]),
            'active_id': None, 'active': None,
            'spring_id': None, 'active_spring': None,
        }
    cached = _gpu_cache[key]
    if cached['active_id'] != settings.node:
        nodes = d['simulation']['nodes']
        cached['active'] = (batch_for_shader(cached['shader'], 'POINTS',
                            {'pos': [nodes[settings.node]['position']]}) if settings.node < len(nodes) else None)
        cached['active_id'] = settings.node
    if cached['spring_id'] != settings.spring:
        springs=d['simulation']['springs']
        cached['active_spring']=(batch_for_shader(cached['shader'],'LINES',
                                 {'pos':[d['simulation']['nodes'][i]['position'] for i in springs[settings.spring]['nodes']]})
                                if settings.spring<len(springs) else None)
        cached['spring_id']=settings.spring
    return cached


def draw_overlay():
    s = getattr(bpy.context.scene, 'vtmb_cloth', None)
    if s is None or not s.show_overlay or s.payload is None:
        return
    import gpu
    try:
        d, stats = document(s)
    except ValueError:
        return
    cached = overlay_batches(s, d)
    shader = cached['shader']

    def draw(name, color):
        batch = cached[name]
        if batch is not None:
            shader.bind()
            shader.uniform_float('color', color)
            batch.draw(shader)

    depth = gpu.state.depth_test_get()
    blend = gpu.state.blend_get()
    try:
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.blend_set('ALPHA')
        if s.show_springs:
            draw('springs', (0.3, 0.8, 0.5, 0.45))
        draw('active_spring', (1.0, 0.1, 0.8, 1.0))
        if s.show_bindings:
            draw('bindings', (0.8, 0.3, 0.9, 0.6))
        gpu.state.point_size_set(5)
        draw('fixed', (0.2, 0.5, 1, 1))
        draw('free', (1, 0.4, 0.05, 1))
        gpu.state.point_size_set(9)
        draw('active', (1, 1, 0, 1))
    finally:
        gpu.state.depth_test_set(depth)
        gpu.state.blend_set(blend)
        gpu.state.point_size_set(1)


class VTMBExport(bpy.types.Operator, ExportHelper):
    bl_idname = 'vtmb_cloth.export_sources'
    bl_label = 'Export REF + VCLOTH'
    filename_ext = '.smd'
    filter_glob: StringProperty(default='*.smd', options={'HIDDEN'})
    check_existing: BoolProperty(default=False, options={'HIDDEN'})

    def invoke(self, context, event):
        from pathlib import Path
        try:
            d, _ = document(context.scene.vtmb_cloth)
            self.filepath=str(Path(context.scene.vtmb_cloth.source_path).with_suffix('.smd'))
        except ValueError as exc:
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def draw(self, context):
        from pathlib import Path
        path=Path(self.filepath)
        self.layout.label(text='Creates a new folder: '+path.stem+'_vtmb_sources')
        self.layout.label(text='REF + VCLOTH only; QC and animations stay in your project.')

    def execute(self, context):
        from pathlib import Path
        path=Path(self.filepath)
        try:
            d, _ = document(context.scene.vtmb_cloth)
            destination=path.parent/(path.stem+'_vtmb_sources')
            report=export_scene.export_sources(context,destination,path.name,d)
        except (ValueError, RuntimeError, OSError, KeyError) as exc:
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}
        self.report({'INFO'},'Validated REF + VCLOTH exported to '+str(destination))
        if report.get('cleanup_warnings'):
            self.report({'WARNING'},'Export succeeded; a locked temporary file will be cleaned up in the background')
        return {'FINISHED'}


_classes = (VTMBTarget, VTMBSettings, VTMBImport, VTMBClear, VTMBRefresh, VTMBAttach,
            VTMBDetach, VTMBValidate, VTMBSelect, VTMBEditSpring, VTMBEditSpringBatch,
            VTMBCaptureCloth, VTMBCaptureAdditionalCloth, VTMBAddClothRegion, VTMBGeneratorAnchors, VTMBGenerateCloth,
            VTMBSetNodeRole, VTMBGeometryBegin, VTMBGeometryUpdate, VTMBGeometryRestore, VTMBExport, VTMBCancelAuthoring, VTMBFinishSelection, VTMBPanel)


def register():
    global _draw_handle
    reload_dependencies()
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.vtmb_cloth = PointerProperty(type=VTMBSettings)
    _draw_handle = bpy.types.SpaceView3D.draw_handler_add(draw_overlay, (), 'WINDOW', 'POST_VIEW')
    for handlers in (bpy.app.handlers.load_post, bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        handlers.append(invalidate_cache)


def unregister():
    global _draw_handle
    for handlers in (bpy.app.handlers.load_post, bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if invalidate_cache in handlers:
            handlers.remove(invalidate_cache)
    if _draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, 'WINDOW')
        _draw_handle = None
    del bpy.types.Scene.vtmb_cloth
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
    invalidate_cache()
    redraw()
