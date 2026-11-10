"""Preserving union of an existing cloth instance and a new disconnected region."""
import copy
import hashlib
import json
import struct
import tempfile
from pathlib import Path
from . import attachment, export, generation, geometry, matching, pose_proof, scene_data, schema, smd
from .schema import require


def partition(base, faces, topology, selected):
    selected = set(selected)
    old_indices = [i for i in range(len(faces)) if i not in selected]
    new_points = {v for i in selected for v in topology[i]}
    old_points = {v for i in old_indices for v in topology[i]}
    require(not new_points.intersection(old_points),
            'Additional cloth must be a disconnected mesh island in the same REF; shared old vertices are unsupported')
    return old_indices


def retained(base, obj, faces, topology, selected):
    remaining = partition(base, faces, topology, selected)
    required = {b['vertex'] for mesh in base['reference']['meshes'] for b in mesh['bindings']}
    required.update(n['reference_vertex'] for n in base['simulation']['nodes'])
    source_faces = [i for i, t in enumerate(base['reference']['triangles']) if required.intersection(t['vertices'])]
    protected = dict(base)
    protected['reference'] = dict(base['reference'], triangles=[base['reference']['triangles'][i] for i in source_faces])
    options = matching.candidate_options(protected, [faces[i] for i in remaining])
    indices = [i for i, candidates in zip(remaining, options) if candidates]
    require(len(indices) == len(source_faces),
            'Existing cloth/support faces are missing, changed or geometrically duplicated. Unrelated rigid faces are not required to match')
    _, owners = scene_data.read_faces([obj])
    old_owners = [owners[i] for i in indices]
    try:
        mapped = scene_data.match_projected(protected, [obj], [faces[i] for i in indices], old_owners,
                                            allow_unmapped=True, protected_vertices=required, geometry_first=True)
    except ValueError as exc:
        if 'exact source-normal projection' in str(exc):
            raise ValueError('Existing cloth has ambiguous source identities and current shading could not resolve them. '
                             'Uniquely matched cloth does not require original shading. ' + str(exc)) from exc
        raise
    require(required <= {v for ids in mapped for v in ids}, 'Existing cloth lost a binding/reference vertex')
    return {'indices': indices, 'mapped': mapped, 'required': required, 'source_faces': source_faces}


def capture(settings, obj, base):
    require(settings.payload is not None and not settings.targets,
            'Import the original base VCLOTH for inspection. Leave the extended REF unattached; detach first if needed')
    require(settings.geometry_session is None, 'Finish the active geometry edit first')
    require(obj is not None and obj.type == 'MESH' and obj.mode == 'OBJECT', 'Select the extended REF in Object Mode')
    require(obj.get(attachment.OWNER) is None and obj.data.get(attachment.OWNER) is None
            and not any(name in obj.data.attributes for name in attachment.ATTRIBUTES),
            'REF has stale/owned cloth attributes; detach their attachment before capture')
    saved = generation.surface_snapshot(obj)
    faces, topology, _ = generation.read_mesh(obj)
    retained(base, obj, faces, topology, saved['faces'])
    saved.update(operation='APPEND', payload_hash=hashlib.sha256(settings.payload.as_string().encode()).hexdigest())
    return geometry.encode_snapshot(saved)


def merge(base, addition, shared_reference=False):
    """Union with stable component-local ordering and reversible index maps.

    Preserve both normal-cache edge partitions, each spring family's ordering,
    every existing SIMD batch and all existing numerical/unknown fields.
    """
    schema.validate(base)
    schema.validate(addition)
    require(struct.pack('<f', base['simulation']['time_step_scale']) ==
            struct.pack('<f', addition['simulation']['time_step_scale']), 'Regions must share the base time-step scale')
    result = copy.deepcopy(base)
    old, new = base['simulation'], addition['simulation']
    old_ref, new_ref = base['reference'], addition['reference']
    old_names = {b['name']: b['id'] for b in old_ref['bones']}
    new_names = {b['id']: b['name'] for b in new_ref['bones']}
    require(set(old_names) == set(new_names.values()), 'Region skeleton bone names differ from the base')
    for bone in new_ref['bones']:
        old_bone = old_ref['bones'][old_names[bone['name']]]
        require((new_names[bone['parent']] if bone['parent'] != -1 else None) ==
                (old_ref['bones'][old_bone['parent']]['name'] if old_bone['parent'] != -1 else None),
                'Region skeleton hierarchy differs from the base')
    bone_map = {i: old_names[name] for i, name in new_names.items()}
    nv, on, nn = (0 if shared_reference else len(old_ref['vertices'])), len(old['nodes']), len(new['nodes'])
    if shared_reference:
        for key in ('bones', 'vertices', 'triangles'):
            require(geometry.canonical(old_ref[key]) == geometry.canonical(new_ref[key]),
                    'Regions do not share the same current REF snapshot')
        require(len(old_ref['meshes']) == len(new_ref['meshes']), 'Shared material layouts differ')
    of, nf = sum(n['fixed'] for n in old['nodes']), sum(n['fixed'] for n in new['nodes'])
    node_old = [i if i < of else i+nf for i in range(on)]
    node_new = [of+i if i < nf else on+i for i in range(nn)]
    total_nodes = on+nn
    op, np = old['normal_edge_prefix'], new['normal_edge_prefix']
    oe, ne = len(old['edges']), len(new['edges'])
    edge_old = [i if i < op else i+np for i in range(oe)]
    edge_new = [op+i if i < np else oe+i for i in range(ne)]
    require(not set(node_old).intersection(node_new) and len(set(node_old+node_new)) == total_nodes,
            'Region node partitions overlap')
    ot = len(old['tangents'])
    sim = result['simulation']
    sim['nodes'] = [None]*total_nodes
    for source, node_map, vertex_offset in ((old, node_old, 0), (new, node_new, nv)):
        for node in source['nodes']:
            record = copy.deepcopy(node)
            record['id'] = node_map[node['id']]
            record['reference_vertex'] += vertex_offset
            sim['nodes'][record['id']] = record
    sim['edges'] = [None]*(oe+ne)
    for source, node_map, edge_map in ((old, node_old, edge_old), (new, node_new, edge_new)):
        for i, edge in enumerate(source['edges']):
            sim['edges'][edge_map[i]] = [node_map[n] for n in edge]
    sim['normal_edge_prefix'] = op+np
    sim['triangles'], sim['tangents'], sim['collision_neighbors'] = [], [], []
    for source, node_map, edge_map in ((old, node_old, edge_old), (new, node_new, edge_new)):
        sim['triangles'].extend([[edge_map[e] for e in t[:2]]+[node_map[n] for n in t[2:]]
                                 for t in source['triangles']])
        for tangent in source['tangents']:
            record = copy.deepcopy(tangent)
            record['edges'] = [edge_map[e] for e in tangent['edges']]
            sim['tangents'].append(record)
        sim['collision_neighbors'].extend([[node_map[n] for n in r] for r in source['collision_neighbors']])
    sim['springs'], old_springs, new_springs = [], [None]*len(old['springs']), [None]*len(new['springs'])
    for family in ('structural', 'bending'):
        for source, node_map, spring_map in ((old, node_old, old_springs), (new, node_new, new_springs)):
            for i, spring in enumerate(source['springs']):
                if spring['family'] == family:
                    record = copy.deepcopy(spring)
                    record['nodes'] = [node_map[n] for n in spring['nodes']]
                    spring_map[i] = len(sim['springs'])
                    sim['springs'].append(record)
    ob, nb = old['structural_batch_count'], new['structural_batch_count']
    sim['batch_sizes'] = old['batch_sizes'][:ob]+new['batch_sizes'][:nb]+old['batch_sizes'][ob:]+new['batch_sizes'][nb:]
    sim['structural_batch_count'] = ob+nb
    ref = result['reference']
    for vertex in ([] if shared_reference else new_ref['vertices']):
        record = copy.deepcopy(vertex)
        record['id'] += nv
        for link in record['skinning']:
            link['bone'] = bone_map[link['bone']]
        if record['exported'] is not None:
            record['exported']['parent_bone'] = bone_map[record['exported']['parent_bone']]
            record['exported']['skinning'] = [[bone_map[b], w] for b, w in record['exported']['skinning']]
        ref['vertices'].append(record)
    for triangle in ([] if shared_reference else new_ref['triangles']):
        record = copy.deepcopy(triangle)
        record['vertices'] = [v+nv for v in triangle['vertices']]
        ref['triangles'].append(record)
    for mesh in ref['meshes']:
        for binding in mesh['bindings']:
            binding['node'] = node_old[binding['node']]
            binding['tangent_index'] = total_nodes+binding['tangent_index']-on
    next_mesh_id = max([m.get('id', -1) for m in ref['meshes'] if type(m.get('id')) is int], default=-1)+1
    for i, mesh in enumerate(new_ref['meshes']):
        record = copy.deepcopy(mesh)
        record['id'] = next_mesh_id+i
        record['vertex_start'] += nv
        for binding in record['bindings']:
            binding['vertex'] += nv
            binding['node'] = node_new[binding['node']]
            binding['tangent_index'] = total_nodes+ot+binding['tangent_index']-nn
        if shared_reference:
            target = ref['meshes'][i]
            require(all(target[k] == mesh[k] for k in ('material', 'vertex_start', 'vertex_count')),
                    'Shared material ranges differ')
            existing = {b['vertex'] for b in target['bindings']}
            require(not existing.intersection(b['vertex'] for b in record['bindings']), 'Existing and added cloth bindings overlap')
            target['bindings'].extend(record['bindings'])
        else:
            ref['meshes'].append(record)
    entry = {'type': 'add_cloth_region', 'old_to_new_nodes': node_old, 'added_to_new_nodes': node_new,
             'old_to_new_edges': edge_old, 'old_to_new_springs': old_springs,
             'added_to_new_springs': new_springs, 'base_nodes': on, 'added_nodes': nn,
             'base_springs': len(old['springs']), 'added_springs': len(new['springs']),
             'shared_time_step_scale': old['time_step_scale'],
             'parameters': copy.deepcopy(addition.get('vtmb_cloth_authoring', {})),
             'preservation_policy': ('unchanged-simulation-protected-geometry-current-shading'
                                     if shared_reference else 'unchanged-base-records-except-explicit-index-remapping')}
    history = result.setdefault('vtmb_cloth_region_history', [])
    require(isinstance(history, list), 'Invalid existing region authoring history')
    history.append(copy.deepcopy(entry))
    schema.validate(result)
    for prefix, mapping in ((op, edge_old), (np, edge_new)):
        require(all((i < prefix) == (target < sim['normal_edge_prefix']) for i, target in enumerate(mapping)),
                'Normal-cache edge partition changed during union')
    verify_preserved(base, result, node_old, edge_old, old_springs, shared_reference=shared_reference)
    return result, entry


def verify_preserved(base, result, node_map, edge_map, spring_map, shared_reference=False):
    """Runtime preservation guard; compare complete records including unknown keys."""
    def same(a, b):
        require(geometry.canonical(a) == geometry.canonical(b), 'Existing cloth preservation check failed')
    a, b = base['reference'], result['reference']
    same(a['vertices'], b['vertices'][:len(a['vertices'])])
    same(a['triangles'], b['triangles'][:len(a['triangles'])])
    total_nodes, original_nodes = len(result['simulation']['nodes']), len(base['simulation']['nodes'])
    for old_mesh, new_mesh in zip(a['meshes'], b['meshes']):
        expected = copy.deepcopy(old_mesh)
        for binding in expected['bindings']:
            binding['node'] = node_map[binding['node']]
            binding['tangent_index'] += total_nodes-original_nodes
        actual = copy.deepcopy(new_mesh)
        if shared_reference:
            existing_vertices = {v['vertex'] for v in expected['bindings']}
            actual['bindings'] = [v for v in actual['bindings'] if v['vertex'] in existing_vertices]
        same(expected, actual)
    for key in a.keys()-{'vertices', 'triangles', 'meshes'}:
        same(a[key], b[key])
    a, b = base['simulation'], result['simulation']
    for node in a['nodes']:
        expected = copy.deepcopy(node)
        expected['id'] = node_map[node['id']]
        same(expected, b['nodes'][expected['id']])
    for i, spring in enumerate(a['springs']):
        expected = copy.deepcopy(spring)
        expected['nodes'] = [node_map[n] for n in spring['nodes']]
        same(expected, b['springs'][spring_map[i]])
    for i, edge in enumerate(a['edges']):
        same([node_map[n] for n in edge], b['edges'][edge_map[i]])
    for triangle, actual in zip(a['triangles'], b['triangles']):
        same([edge_map[e] for e in triangle[:2]]+[node_map[n] for n in triangle[2:]], actual)
    for tangent, actual in zip(a['tangents'], b['tangents']):
        expected = copy.deepcopy(tangent)
        expected['edges'] = [edge_map[e] for e in tangent['edges']]
        same(expected, actual)
    same([[node_map[n] for n in record] for record in a['collision_neighbors']],
         b['collision_neighbors'][:len(a['collision_neighbors'])])
    # Existing batch sizes/order remain intact separately for both families.
    boundary = a['structural_batch_count']
    same(a['batch_sizes'][:boundary], b['batch_sizes'][:boundary])
    same(a['batch_sizes'][boundary:], b['batch_sizes'][b['structural_batch_count']:
                                                           b['structural_batch_count']+len(a['batch_sizes'])-boundary])
    for key in a.keys()-{'nodes', 'springs', 'edges', 'triangles', 'tangents', 'collision_neighbors',
                         'normal_edge_prefix', 'batch_sizes', 'structural_batch_count'}:
        same(a[key], b[key])
    for key in base.keys()-{'reference', 'simulation', 'vtmb_cloth_region_history'}:
        same(base[key], result[key])
    if 'vtmb_cloth_region_history' in base:
        same(base['vtmb_cloth_region_history'], result['vtmb_cloth_region_history'][:-1])


def refresh_reference(base, snapshot, protected):
    """Keep simulation-dependent source data and the artist's current normals."""
    required = protected['required']
    mapped_vertices = {source: set() for source in required}
    assigned = {}
    ref = snapshot['reference']
    for face, ids in zip(protected['indices'], protected['mapped']):
        for source, current in zip(ids, ref['triangles'][face]['vertices']):
            if source not in required:
                continue
            require(current not in assigned or assigned[current] == source,
                    'Current REF merged different protected source identities')
            assigned[current] = source
            mapped_vertices[source].add(current)
    require(all(mapped_vertices.values()), 'An existing cloth/reference record disappeared')
    for current, source in assigned.items():
        record = copy.deepcopy(base['reference']['vertices'][source])
        record['id'] = current
        record['source_normal'] = copy.deepcopy(ref['vertices'][current]['source_normal'])
        record['exported']['normal'] = copy.deepcopy(ref['vertices'][current]['exported']['normal'])
        ref['vertices'][current] = record
    # Extra original reference fields remain preserved; the full original
    # reference (including replaced rigid records/mesh metadata) is archived.
    for key in base['reference'].keys()-{'bones', 'vertices', 'triangles', 'meshes'}:
        ref[key] = copy.deepcopy(base['reference'][key])
    retained_base = copy.deepcopy(base)
    snapshot['vtmb_cloth_reference_normal_policy'] = 'current-decoded-corners-v1'
    retained_base['vtmb_cloth_reference_normal_policy'] = snapshot['vtmb_cloth_reference_normal_policy']
    retained_base['reference'] = copy.deepcopy(ref)
    for mesh in retained_base['reference']['meshes']:
        mesh['bindings'] = []
    for mesh in base['reference']['meshes']:
        for binding in mesh['bindings']:
            for current in sorted(mapped_vertices[binding['vertex']]):
                record = copy.deepcopy(binding)
                record['vertex'] = current
                target = next(m for m in retained_base['reference']['meshes']
                              if m['vertex_start'] <= current < m['vertex_start']+m['vertex_count'])
                target['bindings'].append(record)
    for node in retained_base['simulation']['nodes']:
        node['reference_vertex'] = min(mapped_vertices[node['reference_vertex']])
    archive = retained_base.setdefault('vtmb_cloth_reference_archive', [])
    require(isinstance(archive, list), 'Invalid reference archive')
    archive.append({'operation': 'cloth-scoped-reference-refresh', 'reference': copy.deepcopy(base['reference'])})
    # Protected records retain every field except their new IDs and explicitly
    # authored current normals. No simulation factor, position or role changes.
    for source, current_ids in mapped_vertices.items():
        for current in current_ids:
            expected = copy.deepcopy(base['reference']['vertices'][source])
            expected['id'] = current
            expected['source_normal'] = copy.deepcopy(ref['vertices'][current]['source_normal'])
            expected['exported']['normal'] = copy.deepcopy(ref['vertices'][current]['exported']['normal'])
            require(geometry.canonical(expected) == geometry.canonical(ref['vertices'][current]),
                    'Protected cloth source record changed during reference refresh')
    expected_simulation = copy.deepcopy(base['simulation'])
    for node in expected_simulation['nodes']:
        node['reference_vertex'] = min(mapped_vertices[node['reference_vertex']])
    require(geometry.canonical(expected_simulation) == geometry.canonical(retained_base['simulation']),
            'Existing simulation changed beyond explicit reference remapping')
    schema.validate(snapshot)
    schema.validate(retained_base)
    return retained_base, {str(v): sorted(ids) for v, ids in mapped_vertices.items()}


def add(context, settings, strength, bend_ratio):
    saved, obj, faces, topology = generation.draft(settings, context.scene)
    require(saved.get('operation') == 'APPEND', 'Capture Additional Cloth Faces first')
    base = schema.loads(settings.payload.as_string())
    protected = retained(base, obj, faces, topology, saved['faces'])
    fixed = json.loads(settings.authoring_anchors)
    require(fixed, 'Set Generator Anchors for the additional region first')
    selected = sorted(saved['faces'])
    path = Path(settings.source_path).with_suffix('.smd').resolve()
    source_bytes = path.read_bytes()
    reference = smd.loads(source_bytes.decode('utf-8-sig'))
    export.validate_pose(base, reference)
    authored_faces = copy.deepcopy(faces)
    identities = [[None]*3 for _ in faces]
    for face, ids in zip(protected['indices'], protected['mapped']):
        for corner, source in enumerate(ids):
            if source in protected['required']:
                identities[face][corner] = source
    addition = generation.build(authored_faces, topology, selected, fixed, reference, path.name,
        strength, bend_ratio, base['simulation']['time_step_scale'], corner_identity=identities,
        reference_bones=base['reference']['bones'])
    retained_base, vertex_map = refresh_reference(base, addition, protected)
    merged, entry = merge(retained_base, addition, shared_reference=True)
    entry.update(reference_policy='protect-cloth-geometry-preserve-current-shading',
                 normal_policy='current-decoded-corners-v1',
                 old_to_current_reference_vertices=vertex_map,
                 protected_source_faces=protected['source_faces'],
                 protected_current_faces=protected['indices'])
    merged['vtmb_cloth_region_history'][-1] = copy.deepcopy(entry)
    rig_name = obj.modifiers[0].object.name
    selected_objects = [o.name for o in context.selected_objects]
    active = context.view_layer.objects.active.name if context.view_layer.objects.active else None
    try:
        with tempfile.TemporaryDirectory(prefix='vtmb-cloth-region-') as stage:
            # The old paired REF must still prove the complete ORIGINAL source.
            # Extended geometry is checked separately against old + new records.
            pose_proof.prove(context, obj.modifiers[0].object, path, base, stage)
    finally:
        for current in context.view_layer.objects:
            current.select_set(current.name in selected_objects)
        context.view_layer.objects.active = context.view_layer.objects.get(active) if active else None
    settings = context.scene.vtmb_cloth
    _, obj, _, _ = generation.draft(settings, context.scene)
    require(path.read_bytes() == source_bytes, 'The original REF SMD changed during Add Cloth Region')
    original_normal_bytes = [struct.pack('<3f', *n.vector) for n in obj.data.corner_normals]
    history = json.loads(settings.edit_log)
    require(isinstance(history, list), 'Invalid cloth edit history')
    history.append(entry)
    raw = schema.dumps(merged)
    require(len(raw.encode('utf-8')) <= 64*1024*1024, 'Combined payload/archive exceeds the native 64 MiB source limit')
    import bpy
    backup = obj.data.copy()
    old_payload, old_proof, old_log, old_node, old_spring, old_status = (
        settings.payload, settings.generated_proof, settings.edit_log, settings.node, settings.spring, settings.binding_status)
    payload = proof = None
    try:
        attachment.attach(settings, context.scene, merged, [obj])
        require(original_normal_bytes == [struct.pack('<3f', *n.vector) for n in obj.data.corner_normals],
                'Appending changed current REF shading')
        payload = bpy.data.texts.new('VTMB Cloth: additional region')
        payload.write(raw)
        proof = bpy.data.texts.new('VTMB Cloth: extended reference proof')
        proof.write(json.dumps({'version': 1, 'reference_path': str(path),
            'sha256': hashlib.sha256(source_bytes).hexdigest(), 'rig': rig_name,
            'rest': pose_proof.signature(bpy.data.objects[rig_name]), 'operation': 'append-region',
            'base_payload_sha256': saved['payload_hash']}, indent=2))
        settings.payload, settings.generated_proof = payload, proof
        settings.edit_log = json.dumps(history, allow_nan=False)
        settings.node = entry['old_to_new_nodes'][old_node] if old_node < entry['base_nodes'] else 0
        settings.spring = entry['old_to_new_springs'][old_spring] if old_spring < entry['base_springs'] else 0
        attachment.validate_owned(settings, context.scene, merged)
    except Exception:
        attachment.detach(settings)
        failed = obj.data
        obj.data = backup
        if failed.users == 0:
            bpy.data.meshes.remove(failed)
        settings.payload, settings.generated_proof, settings.edit_log, settings.node, settings.spring = (
            old_payload, old_proof, old_log, old_node, old_spring)
        settings.binding_status = old_status
        for text in (payload, proof):
            if text is not None:
                bpy.data.texts.remove(text)
        raise
    bpy.data.meshes.remove(backup)
    settings.authoring_draft = None
    settings.authoring_anchors = '[]'
    settings.authoring_mode = 'NEW'
    settings.binding_status = f'Added {entry["added_nodes"]} nodes; preserved {entry["base_nodes"]} existing nodes'
    return entry
