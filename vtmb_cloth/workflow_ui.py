"""Guided workflow presentation and explicit Edit Mode selection actions."""
from functools import wraps
import bpy


def selection_action(method):
    """Flush one edited REF for an operator, then restore its editing mode.

    Selection changes are handled by Blender's public mode operator. Never
    read stale Mesh selections while the edit BMesh owns the data. Multiple
    edited objects are rejected instead of changing an implicit target set.
    """
    def run(self, context, *args, **kwargs):
        editing = context.mode == 'EDIT_MESH'
        name = context.object.name if context.object else None
        if editing and len(context.objects_in_mode) != 1:
            self.report({'ERROR'}, 'Edit one REF object at a time for cloth selection actions')
            return {'CANCELLED'}
        try:
            if editing:
                bpy.ops.object.mode_set(mode='OBJECT')
            return method(self, context, *args, **kwargs)
        except (ValueError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        finally:
            if editing:
                obj = context.view_layer.objects.get(name)
                if obj is not None and obj.type == 'MESH' and context.mode == 'OBJECT':
                    context.view_layer.objects.active = obj
                    obj.select_set(True)
                    bpy.ops.object.mode_set(mode='EDIT')
    # Blender validates RNA callbacks by their positional argument counts.
    # Keep explicit signatures rather than registering a variadic wrapper.
    if method.__name__ == 'invoke':
        @wraps(method)
        def invoke(self, context, event):
            return run(self, context, event)
        return invoke
    @wraps(method)
    def execute(self, context):
        return run(self, context)
    return execute


def fold(layout, settings, prop, title):
    box = layout.box()
    row = box.row()
    row.prop(settings, prop, text=title, emboss=False,
             icon='TRIA_DOWN' if getattr(settings, prop) else 'TRIA_RIGHT')
    return box if getattr(settings, prop) else None


def draw_authoring(layout, context, s, append):
    box = layout.box()
    box.label(text='Add Cloth Region' if append else 'Create New Cloth')
    if append and s.payload is None:
        box.label(text='Load the original VCLOTH first.', icon='INFO')
        box.operator('vtmb_cloth.import_file', text='Load Original Cloth')
        return
    if s.targets:
        box.label(text='Detach the REF mapping before capturing a surface.')
        box.label(text='The loaded cloth data will be kept.')
        if context.mode == 'EDIT_MESH':
            box.operator('vtmb_cloth.finish_selection')
        row = box.row()
        row.enabled = context.mode == 'OBJECT'
        row.operator('vtmb_cloth.detach', text='Detach REF Mapping')
        return
    if not append and s.payload is not None:
        box.label(text='A cloth setup is already loaded.', icon='INFO')
        box.label(text='Use Add Cloth Region to keep it.')
        if context.mode == 'EDIT_MESH':
            box.operator('vtmb_cloth.finish_selection')
        row = box.row()
        row.enabled = context.mode == 'OBJECT'
        row.operator('vtmb_cloth.clear', text='Unload Data to Create New Cloth')
        return
    mode = 'APPEND' if append else 'NEW'
    draft = s.authoring_draft is not None and s.authoring_mode == mode
    if s.authoring_draft is not None and not draft:
        box.label(text='Another creation workflow has a captured surface.')
        box.operator('vtmb_cloth.cancel_authoring', text='Discard Captured Selection')
        return
    capture = 'vtmb_cloth.capture_additional_surface' if append else 'vtmb_cloth.capture_surface'
    if not draft:
        box.label(text='1. Select the entire cloth surface', icon='MESH_DATA')
        box.label(text='Select faces in Edit Mode.')
        if append:
            box.label(text='Only the new region; exclude existing cloth.')
        box.operator(capture, text='Capture Surface')
        return
    box.label(text='1. Surface captured', icon='CHECKMARK')
    if s.captured_face_count:
        box.label(text=f'{s.captured_face_count} triangles captured')
        box.label(text=s.captured_object)
    box.operator(capture, text='Change Surface (resets attachment points)')
    anchors = s.authoring_anchors != '[]'
    box.separator()
    box.label(text='2. Select attachment points', icon='CHECKMARK' if anchors else 'VERTEXSEL')
    box.label(text='Select vertices that follow the character.')
    box.label(text='The remaining cloth vertices can move.')
    if anchors and s.captured_anchor_count:
        box.label(text=f'{s.captured_anchor_count} attachment points recorded')
    box.operator('vtmb_cloth.generator_anchors',
                 text='Change Attachment Points' if anchors else 'Set Attachment Points')
    if anchors:
        box.separator()
        box.label(text='3. Generate cloth')
        if append:
            box.label(text='Existing cloth and current shading are kept.')
        if context.mode == 'EDIT_MESH':
            box.operator('vtmb_cloth.finish_selection', text='Finish Selection')
        elif context.mode != 'OBJECT':
            box.label(text='Switch to Object Mode to generate.', icon='INFO')
        row = box.row()
        row.enabled = context.mode == 'OBJECT'
        row.operator('vtmb_cloth.add_region' if append else 'vtmb_cloth.generate',
                     text='Generate Added Cloth' if append else 'Generate Cloth - Choose REF SMD')
    box.separator()
    box.operator('vtmb_cloth.cancel_authoring', text='Cancel Creation')


def draw_edit(layout, context, s):
    if s.payload is None:
        layout.label(text='Load a VCLOTH file to edit existing cloth.', icon='INFO')
        layout.operator('vtmb_cloth.import_file', text='Load Cloth Data')
        return
    if not s.targets:
        box = layout.box()
        box.label(text='Select the original REF object in Object Mode.')
        if context.mode == 'EDIT_MESH':
            box.operator('vtmb_cloth.finish_selection')
        row = box.row()
        row.enabled = context.mode == 'OBJECT'
        row.operator('vtmb_cloth.attach', text='Connect Selected REF')
        return
    box = layout.box()
    box.label(text='Shape')
    if s.geometry_session is None:
        if context.mode == 'EDIT_MESH':
            box.operator('vtmb_cloth.finish_selection', text='Finish Selection to Start Shape Edit')
        box.operator('vtmb_cloth.geometry_begin', text='Start Shape Edit')
    else:
        box.label(text='Move existing vertices; keep faces, UVs and weights.')
        if context.mode == 'EDIT_MESH':
            box.operator('vtmb_cloth.finish_selection', text='Finish Moving Vertices')
        box.operator('vtmb_cloth.geometry_update', text='Apply Shape Changes')
        box.operator('vtmb_cloth.geometry_restore', text='Restore Previous Shape')
    box = layout.box()
    box.label(text='Attachment')
    box.label(text='Select cloth vertices in Edit Mode.')
    box.operator('vtmb_cloth.set_node_role', text='Attach to Character').role = 'FIXED'
    box.operator('vtmb_cloth.set_node_role', text='Allow Cloth Movement').role = 'FREE'
    box = layout.box()
    box.label(text='Parameters')
    box.label(text='Scale existing factors on selected vertices.')
    box.label(text='Raw factors; not a calibrated stiffness setting.')
    box.operator('vtmb_cloth.edit_spring_batch', text='Edit Selected Cloth Factors')
    layout.operator_menu_enum('vtmb_cloth.select_bound', 'role', text='Select Cloth Vertices')


def draw_panel(layout, context, api):
    s = context.scene.vtmb_cloth
    # A saved legacy draft has no workflow UI property. Infer its visible task
    # without parsing Text or mutating persistent settings during redraw.
    workflow = ('EDIT' if s.geometry_session is not None else
                ('APPEND' if s.authoring_mode == 'APPEND' else 'NEW')
                if s.authoring_draft is not None else s.workflow)
    if s.authoring_draft is not None or s.geometry_session is not None:
        names = {'EDIT': 'Edit Existing Cloth', 'APPEND': 'Add Cloth Region', 'NEW': 'Create New Cloth'}
        layout.label(text='Task: ' + names[workflow])
    else:
        layout.prop(s, 'workflow', text='Task')
    d = stats = None
    if s.payload is not None:
        try:
            d, stats = api.document(s)
        except ValueError as exc:
            layout.label(text=str(exc), icon='ERROR')
            layout.operator('vtmb_cloth.refresh')
            layout.operator('vtmb_cloth.clear')
            return
        status = layout.box()
        status.label(text=d['reference_smd'], icon='FILE_3D')
        status.label(text='REF connected' if s.targets else 'Cloth data loaded')
        status.label(text=s.binding_status)
    if workflow == 'EDIT':
        draw_edit(layout, context, s)
    else:
        draw_authoring(layout, context, s, workflow == 'APPEND')
    if s.targets:
        box = layout.box()
        box.label(text='Export')
        if context.mode == 'EDIT_MESH':
            box.operator('vtmb_cloth.finish_selection', text='Finish Editing')
        if s.geometry_session is not None:
            box.label(text='Apply or restore shape changes before export.')
        row = box.row()
        row.enabled = context.mode == 'OBJECT' and s.geometry_session is None
        row.operator('vtmb_cloth.export_sources', text='Export REF + VCLOTH', icon='EXPORT')
    if d is None:
        return
    box = fold(layout, s, 'ui_display', 'Viewport Display')
    if box:
        box.prop(s, 'show_overlay', text='Show Cloth Structure')
        box.prop(s, 'show_springs')
        box.prop(s, 'show_bindings')
        box.label(text='Blue: attached / Orange: free / Yellow: active')
        box.label(text='Static bind-pose display; motion is tested in game.')
    box = fold(layout, s, 'ui_advanced', 'Advanced / Diagnostics')
    if not box:
        return
    for key, value in stats.items():
        box.label(text=f'{key.title()}: {value}')
    row = box.row()
    row.enabled = bool(s.targets) and context.mode == 'OBJECT'
    row.operator('vtmb_cloth.validate_attachment', text='Validate REF Connection')
    box.operator('vtmb_cloth.refresh')
    node = box.box()
    node.prop(s, 'node')
    nodes = d['simulation']['nodes']
    if s.node < len(nodes):
        n = nodes[s.node]
        node.label(text='Attached' if n['fixed'] else 'Free')
        node.label(text=f'Reference vertex: {n["reference_vertex"]}')
        node.label(text='Position: ' + ', '.join(f'{v:.9g}' for v in n['position']))
        count = api._binding_counts[s.payload.as_pointer()].get(s.node, 0)
        node.label(text=f'Render bindings: {count}')
    else:
        node.label(text='Node ID outside table', icon='ERROR')
    spring = box.box()
    spring.prop(s, 'spring')
    springs = d['simulation']['springs']
    if s.spring < len(springs):
        record = springs[s.spring]
        spring.label(text=f'Nodes: {record["nodes"]} ({record["family"]})')
        for key in ('weight_a', 'weight_b', 'rest_length_squared'):
            spring.label(text=f'{key}: {record[key]:.9g}')
        row = spring.row()
        row.enabled = bool(s.targets) and context.mode == 'OBJECT'
        row.operator('vtmb_cloth.edit_spring')
    else:
        spring.label(text='Spring ID outside table', icon='ERROR')
    row = box.column()
    row.enabled = context.mode == 'OBJECT' and s.geometry_session is None
    row.operator('vtmb_cloth.import_file', text='Load Another VCLOTH')
    row.operator('vtmb_cloth.detach')
    row.operator('vtmb_cloth.clear')
