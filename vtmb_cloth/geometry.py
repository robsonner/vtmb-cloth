"""Explicit position-only authoring on an owned, unsplit REF.

Rest-length ratios and tangent magnitudes are preserved as an authoring policy,
not a recovered Troika authoring formula. No topology or solver is generated.
"""
import copy
import json
import math
import struct
import base64
import zlib
from array import array
from . import attachment, editing, pose_proof, scene_data, schema
from .schema import require


def sub(a, b):
    return [x-y for x, y in zip(a, b)]


def cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def squared(v):
    return sum(x*x for x in v)


def canonical(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(',', ':'))


def encode_snapshot(value):
    """Keep Text lines bounded; never put megabytes on one editor line."""
    packed = zlib.compress(canonical(value).encode('utf-8'))
    encoded = base64.b64encode(packed).decode('ascii')
    return 'VTMB-GEOMETRY-SNAPSHOT-1\n' + '\n'.join(encoded[i:i+4096] for i in range(0, len(encoded), 4096)) + '\n'


def decode_snapshot(text):
    raw = text.as_string()
    if raw.startswith('VTMB-GEOMETRY-SNAPSHOT-1\n'):
        try:
            packed = base64.b64decode(''.join(raw.splitlines()[1:]), validate=True)
            decoder = zlib.decompressobj()
            decoded = decoder.decompress(packed, 256*1024*1024)
            require(decoder.eof and not decoder.unconsumed_tail and not decoder.unused_data,
                    'Invalid or oversized geometry snapshot')
            return json.loads(decoded)
        except (ValueError, zlib.error) as exc:
            raise ValueError(f'Cannot read geometry snapshot: {exc}') from exc
    # Existing 0.5.0 blend files can still complete or restore their session.
    return json.loads(raw)


def mesh_state(obj):
    """Capture invariants once per explicit operation, never during redraw."""
    mesh = obj.data
    require(not obj.constraints, 'REF object constraints are unsupported')
    keys = mesh.shape_keys
    require(keys is None or all(k.value == 0 for k in keys.key_blocks),
            'Set shape-key values to zero before geometry editing')
    require(keys is None or obj.active_shape_key_index == 0,
            'Select the Basis shape key before geometry editing')
    require(keys is None or keys.use_relative, 'Absolute shape keys are unsupported for geometry editing')
    modifiers = list(obj.modifiers)
    require(len(modifiers) == 1 and modifiers[0].type == 'ARMATURE' and modifiers[0].object,
            'Geometry editing requires only the original Armature modifier')
    modifier = modifiers[0]
    require(modifier.use_vertex_groups and not modifier.use_bone_envelopes and not modifier.vertex_group,
            'Unsupported Armature skinning settings')
    rig = modifier.object
    require(not rig.constraints and all(not b.constraints for b in rig.pose.bones),
            'Armature constraints are unsupported')
    return {
        'vertices': len(mesh.vertices), 'edges': [list(e.vertices) for e in mesh.edges],
        'loops': [l.vertex_index for l in mesh.loops],
        'polygons': [[p.loop_start, p.loop_total, p.material_index, p.use_smooth] for p in mesh.polygons],
        'materials': [m.name if m else None for m in mesh.materials],
        'attributes': {name: [list(v.vector) if kind == 'FLOAT_VECTOR' else v.value
                              for v in mesh.attributes[name].data]
                       for name, kind in attachment.ATTRIBUTES.items()},
        'rig': rig.name, 'rest': pose_proof.signature(rig),
        'rig_matrix': [list(row) for row in rig.matrix_world],
        'modifier': [modifier.use_deform_preserve_volume, modifier.show_viewport, modifier.show_render],
        'keys': None if keys is None else {
            'relative': keys.use_relative,
            'blocks': [[k.name, k.relative_key.name, k.mute, k.vertex_group] for k in keys.key_blocks]},
    }


def key_positions(obj):
    keys = obj.data.shape_keys
    result = []
    if keys is not None:
        for key in keys.key_blocks:
            values = array('f', [0]) * (len(key.data)*3)
            key.data.foreach_get('co', values)
            result.append([list(values[i:i+3]) for i in range(0, len(values), 3)])
    return result


def pack_keys(keys):
    """Avoid serializing millions of shape-key coordinates as JSON numbers."""
    result = []
    for positions in keys:
        values = array('f', (v for position in positions for v in position))
        if struct.pack('=I', 1) != struct.pack('<I', 1):
            values.byteswap()
        result.append(base64.b64encode(values.tobytes()).decode('ascii'))
    return {'encoding': 'float32-le-base64', 'blocks': result}


def unpack_keys(value):
    if isinstance(value, list):
        return value
    require(value['encoding'] == 'float32-le-base64', 'Unknown shape-key snapshot format')
    result = []
    for block in value['blocks']:
        values = array('f')
        values.frombytes(base64.b64decode(block, validate=True))
        require(len(values) % 3 == 0, 'Malformed shape-key snapshot')
        if struct.pack('=I', 1) != struct.pack('<I', 1):
            values.byteswap()
        result.append([list(values[i:i+3]) for i in range(0, len(values), 3)])
    return result


def begin(settings, scene, document):
    objects, faces = attachment.validate_owned(settings, scene, document)
    require(len(objects) == 1, 'Geometry editing currently supports one unsplit REF')
    obj = objects[0]
    state = mesh_state(obj)
    keys = key_positions(obj)
    require(not keys or keys[0] == [list(v.co) for v in obj.data.vertices],
            'Mesh positions must match the neutral Basis shape key')
    return encode_snapshot({'document': document, 'digest': settings.binding_digest,
                      'token': settings.binding_token, 'object': obj.name, 'state': state,
                      'positions': [list(v.co) for v in obj.data.vertices],
                      'keys': pack_keys(keys), 'faces': faces})


def rebuild(document, positions):
    """Rebase mapped positions; retain identities, factors, batches and topology."""
    result = copy.deepcopy(document)
    old = document['reference']['vertices']
    vertices = result['reference']['vertices']
    deltas, moved = {}, set()
    for source, position in positions.items():
        baseline = [editing.float32(v) for v in old[source]['exported']['position']]
        delta = sub(position, baseline)
        deltas[source] = delta
        if squared(delta) == 0:
            continue
        moved.add(source)
        # Keep the authored float32 coordinates. The controlled SMD adapter
        # verifies the exporter's exact decimal conversion before replaying
        # these positions, just as it replays preserved normals and weights.
        vertices[source]['exported']['position'] = list(position)
        vertices[source]['source_position'] = [editing.float32(v+d) for v, d in zip(old[source]['source_position'], delta)]
        require(all(abs(v-editing.float32(e)) <= 0.6e-6 for v, e in
                    zip(vertices[source]['source_position'], vertices[source]['exported']['position'])),
                f'Vertex {source}: position cannot retain source precision within the native REF contract')
    if not moved:
        return result, {'type': 'geometry', 'vertices': 0, 'nodes': 0, 'springs': 0, 'tangents': 0}
    simulation = result['simulation']
    changed_nodes = set()
    for node in simulation['nodes']:
        source = node['reference_vertex']
        if source in moved:
            node['position'] = list(vertices[source]['source_position'])
            changed_nodes.add(node['id'])
    # A node may have many render aliases, plus a separate skinning reference.
    # Require their actual edit displacements to agree; never use proximity.
    for mesh in document['reference']['meshes']:
        for binding in mesh['bindings']:
            source = binding['vertex']
            ref = simulation['nodes'][binding['node']]['reference_vertex']
            require(deltas[source] == deltas[ref],
                    f'Node {binding["node"]}: move all bound aliases and its reference vertex together')
    springs_changed = 0
    before_nodes = document['simulation']['nodes']
    for spring in simulation['springs']:
        a, b = spring['nodes']
        if not ({a, b} & changed_nodes):
            continue
        before = squared(sub(before_nodes[b]['position'], before_nodes[a]['position']))
        after = squared(sub(simulation['nodes'][b]['position'], simulation['nodes'][a]['position']))
        require(before > 0 and after > 0, 'Collapsed spring; geometry update rejected')
        # Preserve the original prestress ratio, including bending thresholds.
        spring['rest_length_squared'] = editing.float32(spring['rest_length_squared'] * after / before)
        require(spring['rest_length_squared'] > 0, 'Spring rest length underflow')
        springs_changed += 1
    old_edges = [sub(before_nodes[b]['position'], before_nodes[a]['position']) for a, b in simulation['edges']]
    new_edges = [sub(simulation['nodes'][b]['position'], simulation['nodes'][a]['position']) for a, b in simulation['edges']]
    for before, after in zip(old_edges, new_edges):
        if before != after:
            require(squared(after) > 0, 'Collapsed simulation edge; geometry update rejected')
    for triangle in simulation['triangles']:
        e0, e1 = triangle[:2]
        if old_edges[e0] != new_edges[e0] or old_edges[e1] != new_edges[e1]:
            require(squared(cross(new_edges[e0], new_edges[e1])) > 0,
                    'Collapsed simulation triangle; geometry update rejected')
    tangents_changed = 0
    for tangent in simulation['tangents']:
        a, b = tangent['edges']
        if old_edges[a] == new_edges[a] and old_edges[b] == new_edges[b]:
            continue
        ca, cb = tangent['coefficients']
        before = squared([ca*x+cb*y for x, y in zip(old_edges[a], old_edges[b])])
        after = squared([ca*x+cb*y for x, y in zip(new_edges[a], new_edges[b])])
        require(before > 0 and after > 0, 'Degenerate tangent basis; geometry update rejected')
        scale = math.sqrt(before/after)
        tangent['coefficients'] = [editing.float32(c*scale) for c in (ca, cb)]
        require(squared([tangent['coefficients'][0]*x+tangent['coefficients'][1]*y
                         for x, y in zip(new_edges[a], new_edges[b])]) > 0,
                'Tangent coefficient underflow')
        tangents_changed += 1
    # Recompute geometric shading only for corners on affected source vertices.
    affected = {v for t in result['reference']['triangles'] if moved.intersection(t['vertices']) for v in t['vertices']}
    sums = {v: [0.0]*3 for v in affected}
    for triangle in result['reference']['triangles']:
        ids = triangle['vertices']
        if not affected.intersection(ids):
            continue
        a, b, c = [vertices[v]['exported']['position'] for v in ids]
        normal = cross(sub(b, a), sub(c, a))
        require(squared(normal) > 0, 'Collapsed render triangle; geometry update rejected')
        for v in ids:
            if v in sums:
                sums[v] = [x+y for x, y in zip(sums[v], normal)]
    for v, normal in sums.items():
        length = math.sqrt(squared(normal))
        require(length > 0, f'Vertex {v}: opposing faces cancel its authored normal')
        normal = [editing.float32(x/length) for x in normal]
        vertices[v]['source_normal'] = normal
        vertices[v]['exported']['normal'] = list(normal)
    schema.validate(result)
    return result, {'type': 'geometry', 'vertices': len(moved), 'nodes': len(changed_nodes),
                    'springs': springs_changed, 'tangents': tangents_changed,
                    'rest_policy': 'preserve-rest-squared-to-distance-squared-ratio',
                    'tangent_policy': 'retain-edge-basis-and-reference-vector-magnitude',
                    'normal_policy': 'area-weighted-affected-source-identities'}


def prepare(settings, scene, document):
    """Validate edits against the trusted Begin snapshot without changing RNA."""
    require(settings.geometry_session is not None, 'Choose Begin Geometry Edit before moving vertices')
    baseline = decode_snapshot(settings.geometry_session)
    require(baseline['token'] == settings.binding_token and baseline['digest'] == settings.binding_digest
            and baseline['document'] == document, 'Cloth attachment/data changed; restore it before updating geometry')
    require(attachment.fingerprint(baseline['document'], baseline['faces']) == baseline['digest'],
            'Geometry baseline was modified; restore the original snapshot')
    objects = attachment.targets(settings, scene)
    require(len(objects) == 1 and objects[0].name == baseline['object'], 'Geometry edit target changed')
    obj = objects[0]
    faces, owners = scene_data.read_faces(objects)
    require(canonical(mesh_state(obj)) == canonical(baseline['state']),
            'Topology, provenance, materials, shape-key setup or rig changed; only vertex movement is supported')
    require(len(faces) == len(baseline['faces']), 'Face count changed')
    for face_index, (face, old_face) in enumerate(zip(faces, baseline['faces'])):
        require(face['material'] == old_face['material'], 'Material changed')
        for corner_index, (corner, old_corner) in enumerate(zip(face['corners'], old_face['corners'])):
            require(corner['uv'] == old_corner['uv'],
                    f'Face {face_index}, corner {corner_index}: UVs changed; only positions may be edited')
            # JSON snapshots decode tuple links as lists. Compare the same
            # representation without changing order, names or numeric values.
            require([list(link) for link in corner['skinning']] == old_corner['skinning'],
                    f'Face {face_index}, corner {corner_index}: real skin weights changed; only positions may be edited')
    positions = [list(v.co) for v in obj.data.vertices]
    # Basis edits may translate relative keys. Every key must preserve its
    # original offset from Basis; facial-shape authoring is outside this feature.
    for before, after in zip(unpack_keys(baseline['keys']), key_positions(obj)):
        for i, (old, new) in enumerate(zip(before, after)):
            expected = [editing.float32(x+y-z) for x, y, z in zip(old, positions[i], baseline['positions'][i])]
            require(new == expected, 'Shape-key offsets changed; edit only Basis with neutral keys')
    import bpy
    scratch = obj.data.copy()
    try:
        for vertex, position in zip(scratch.vertices, baseline['positions']):
            vertex.co = position
        # Mesh corner_normals uses base positions, not the posed depsgraph.
        scratch.update()
        normal_bytes = [struct.pack('<3f', *n.vector) for n in scratch.corner_normals]
        for (_, loops), old_face in zip(owners, baseline['faces']):
            require(all(normal_bytes[l] == struct.pack('<3f', *c['normal'])
                        for l, c in zip(loops, old_face['corners'])),
                    'Custom normals or shading changed independently of geometry')
    finally:
        bpy.data.meshes.remove(scratch)
    mapped = {}
    for (owner, loops), face in zip(owners, faces):
        for loop, corner in zip(loops, face['corners']):
            source = owner.data.attributes['vtmb_source_vertex'].data[loop].value
            position = corner['position']
            require(source not in mapped or mapped[source] == position,
                    f'Source vertex {source} was split into conflicting positions')
            mapped[source] = position
    return obj, rebuild(document, mapped)


def apply(settings, scene, document):
    """Publish payload, normals and digest together, rolling back on failure."""
    obj, (updated, entry) = prepare(settings, scene, document)
    if not entry['vertices']:
        settings.geometry_session = None
        settings.binding_status = 'Geometry unchanged; edit session closed'
        return entry
    import bpy
    mesh = obj.data
    # Preserve encoded custom normals exactly on rollback, rather than trying
    # to invert Blender's lossy custom-normal encoding through its setter.
    backup = mesh.copy()
    arrays = [updated['reference']['vertices'][v.value]['exported']['normal']
              for v in mesh.attributes['vtmb_source_vertex'].data]
    payload = None
    old_payload, old_digest, old_log = settings.payload, settings.binding_digest, settings.edit_log
    try:
        # Author the new normals explicitly, then retain the unchanged exact
        # normal-projection validator. Never widen its matching tolerances.
        mesh.normals_split_custom_set(arrays)
        mesh.update()
        for item, normal in zip(mesh.attributes['vtmb_source_normal'].data, arrays):
            item.vector = normal
        faces, _ = scene_data.read_faces([obj])
        digest = attachment.fingerprint(updated, faces)
        history = json.loads(settings.edit_log)
        require(isinstance(history, list), 'Invalid edit history')
        history.append(entry)
        raw = schema.dumps(updated)
        payload = bpy.data.texts.new('VTMB Cloth: geometry edit')
        payload.write(raw)
        settings.payload = payload
        settings.binding_digest = digest
        settings.edit_log = json.dumps(history, allow_nan=False)
        attachment.validate_owned(settings, scene, updated)
    except Exception:
        obj.data = backup
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)
        settings.payload, settings.binding_digest, settings.edit_log = old_payload, old_digest, old_log
        if payload is not None:
            bpy.data.texts.remove(payload)
        raise
    bpy.data.meshes.remove(backup)
    settings.geometry_session = None
    settings.binding_status = f'Geometry updated: {entry["vertices"]} source vertices, {entry["nodes"]} nodes'
    return entry
