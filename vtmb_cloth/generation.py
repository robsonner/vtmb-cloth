"""Generate v1 simulation data from an explicit surface on the original REF.

All generation choices are project authoring policies, not stock recovery.
No welding, nearest matching, new mesh objects, artificial bones or solver.
"""
import hashlib
import copy
import json
import math
import tempfile
from collections import defaultdict
from pathlib import Path
from . import attachment, editing, geometry, pose_proof, roles, scene_data, schema, smd
from .schema import require


def dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def unit(v, message):
    length = math.sqrt(dot(v, v))
    require(length > 0 and math.isfinite(length), message)
    return [x/length for x in v]


def read_mesh(obj):
    require(obj is not None and obj.type == 'MESH' and obj.mode == 'OBJECT', 'Select the REF in Object Mode')
    require(not obj.constraints, 'REF constraints are unsupported')
    require(len(obj.modifiers) == 1 and obj.modifiers[0].type == 'ARMATURE' and obj.modifiers[0].object,
            'REF needs only its real Armature modifier')
    modifier = obj.modifiers[0]
    require(modifier.use_vertex_groups and not modifier.use_bone_envelopes and not modifier.vertex_group,
            'Unsupported Armature skinning settings')
    rig = modifier.object
    require(not rig.constraints and all(not b.constraints for b in rig.pose.bones), 'Armature constraints are unsupported')
    keys = obj.data.shape_keys
    require(keys is None or (obj.active_shape_key_index == 0 and all(k.value == 0 for k in keys.key_blocks)),
            'Use neutral shape keys with Basis active')
    faces, owners = scene_data.read_faces([obj])
    topology = [[obj.data.loops[l].vertex_index for l in loops] for _, loops in owners]
    state = {'faces': faces, 'topology': topology, 'positions': [list(v.co) for v in obj.data.vertices],
             'rig': rig.name, 'rest': pose_proof.signature(rig),
             'rig_matrix': [list(row) for row in rig.matrix_world],
             'modifier': [modifier.use_deform_preserve_volume, modifier.show_viewport, modifier.show_render]}
    return faces, topology, hashlib.sha256(geometry.canonical(state).encode()).hexdigest()


def capture(settings, obj):
    require(obj is not None and obj.type == 'MESH' and obj.mode == 'OBJECT', 'Select the REF in Object Mode')
    require(not settings.targets and settings.payload is None,
            'Unload the existing cloth data before creating a new setup; keep a copy of its sources')
    require(obj.get(attachment.OWNER) is None and obj.data.get(attachment.OWNER) is None,
            'REF is already owned; detach its cloth setup first')
    require(not any(name in obj.data.attributes for name in attachment.ATTRIBUTES), 'Remove stale owned cloth attributes first')
    return geometry.encode_snapshot(surface_snapshot(obj))


def surface_snapshot(obj):
    faces, topology, digest = read_mesh(obj)
    selected = [p.index for p in obj.data.polygons if p.select]
    require(selected, 'Select cloth faces in Edit Mode, then return to Object Mode')
    points = sorted({v for i in selected for v in topology[i]})
    surface = set(selected)
    outside = {v for i, ids in enumerate(topology) if i not in surface for v in ids}
    point_set = set(points)
    require(not any(i not in surface and set(ids) <= point_set for i, ids in enumerate(topology)),
            'Selection omits a face entirely inside the cloth points; include that face explicitly')
    return {'object': obj.name, 'digest': digest, 'faces': selected,
            'points': points, 'boundary': sorted(set(points) & outside)}


def draft(settings, scene):
    require(settings.authoring_draft is not None, 'Capture Cloth Faces first')
    saved = geometry.decode_snapshot(settings.authoring_draft)
    if saved.get('operation') == 'APPEND':
        require(settings.payload is not None and not settings.targets,
                'Keep the base VCLOTH loaded without attaching the extended REF')
        require(hashlib.sha256(settings.payload.as_string().encode()).hexdigest() == saved['payload_hash'],
                'Base VCLOTH changed; capture the additional region again')
    else:
        require(settings.payload is None and not settings.targets, 'Unload existing cloth data before generation')
    obj = scene.objects.get(saved['object'])
    require(obj is not None, 'Captured REF is missing')
    faces, topology, digest = read_mesh(obj)
    require(digest == saved['digest'], 'REF geometry, shading, UVs, skinning or rig changed; capture cloth faces again')
    return saved, obj, faces, topology


def anchors(settings, scene):
    saved, obj, _, _ = draft(settings, scene)
    point_set = set(saved['points'])
    selected = sorted(v.index for v in obj.data.vertices if v.select and v.index in point_set)
    require(selected and len(selected) < len(saved['points']), 'Select some, but not all, cloth points as anchors')
    missing = set(saved['boundary']) - set(selected)
    require(not missing, f'Include all rigid/cloth boundary points in the anchors; {len(missing)} are missing')
    return selected


def bones_from_reference(reference):
    ids = {old: new for new, old in enumerate(sorted(reference['bones']))}
    bones = []
    for old, new in ids.items():
        bone, pose = reference['bones'][old], reference['pose'][old]
        x, y, z = [v/2 for v in pose[3:]]
        sx, sy, sz, cx, cy, cz = math.sin(x), math.sin(y), math.sin(z), math.cos(x), math.cos(y), math.cos(z)
        bones.append({'id': new, 'name': bone['name'], 'parent': ids[bone['parent']] if bone['parent'] != -1 else -1,
                      'position': list(pose[:3]),
                      'rotation_xyzw': [sx*cy*cz-cx*sy*sz, cx*sy*cz+sx*cy*sz,
                                        cx*cy*sz-sx*sy*cz, cx*cy*cz+sx*sy*sz]})
    return bones


def weights(corner, bone_ids):
    merged = defaultdict(float)
    for name, weight in corner['skinning']:
        if weight > 0:
            require(name in bone_ids, f'Weighted bone {name} is missing from the chosen REF SMD')
            merged[bone_ids[name]] += weight
    require(0 < len(merged) <= 3, 'Every REF point needs 1–3 real bone influences; no pruning is performed')
    total = sum(merged.values())
    links = [[bone, weight/total] for bone, weight in sorted(merged.items())]
    exact = [weight*255 for _, weight in links]
    quantized = [math.floor(v) for v in exact]
    for i in sorted(range(len(links)), key=lambda i: (-(exact[i]-quantized[i]), links[i][0]))[:255-sum(quantized)]:
        quantized[i] += 1
    return links, [{'bone': b, 'weight_byte': w} for (b, _), w in zip(links, quantized)]


def build(faces, topology, selected_faces, fixed_points, reference, reference_name,
          strength=2.0, bend_ratio=0.7, time_scale=3.0, corner_identity=None, reference_bones=None):
    """Pure deterministic generation; IDs derive from captured mesh topology."""
    for value in (strength, bend_ratio, time_scale):
        require(schema.scalar(value) > 0, 'Generation parameters must be finite and positive')
    require(bend_ratio <= 1, 'Bending rest ratio must be at most one')
    surface = set(selected_faces)
    points = sorted({v for i in surface for v in topology[i]})
    fixed = set(fixed_points)
    require(fixed < set(points) and fixed, 'Choose a nonempty proper subset of cloth points as fixed anchors')
    order = [v for v in points if v in fixed]+[v for v in points if v not in fixed]
    node_for_point = {v: i for i, v in enumerate(order)}
    bones = copy.deepcopy(reference_bones) if reference_bones is not None else bones_from_reference(reference)
    bone_ids = {b['name']: b['id'] for b in bones}
    vertices, triangles, meshes, ids_for_face = [], [], [], [None]*len(faces)
    record_points = {}
    for material in dict.fromkeys(f['material'] for f in faces):
        start, lookup = len(vertices), {}
        for i, face in enumerate(faces):
            if face['material'] != material:
                continue
            ids = []
            for corner_index, (corner, point) in enumerate(zip(face['corners'], topology[i])):
                links, skin = weights(corner, bone_ids)
                identity = corner_identity[i][corner_index] if corner_identity is not None else None
                key = geometry.canonical([point, corner['normal'], corner['uv'], links, identity])
                if key not in lookup:
                    source = len(vertices)
                    lookup[key] = source
                    record_points[source] = point
                    vertices.append({'id': source, 'source_position': list(corner['position']),
                                     'source_normal': list(corner['normal']), 'source_uv': list(corner['uv']),
                                     'skinning': skin,
                                     'exported': {'position': list(corner['position']), 'normal': list(corner['normal']),
                                                  'uv': list(corner['uv']), 'parent_bone': max(links, key=lambda p: p[1])[0],
                                                  'skinning': links}})
                ids.append(lookup[key])
            ids_for_face[i] = ids
        meshes.append({'id': len(meshes), 'material': material, 'vertex_start': start,
                       'vertex_count': len(vertices)-start, 'bindings': []})
    triangles = [{'material': face['material'], 'vertices': ids_for_face[i]} for i, face in enumerate(faces)]
    source_for_point = {}
    for source, point in record_points.items():
        source_for_point.setdefault(point, source)
    nodes = [{'id': i, 'fixed': point in fixed, 'reference_vertex': source_for_point[point],
              'position': list(vertices[source_for_point[point]]['source_position'])} for i, point in enumerate(order)]
    edges, edge_ids, sim_triangles, opposites = [], {}, [], defaultdict(list)
    def edge(a, b):
        if (a, b) not in edge_ids:
            edge_ids[a, b] = len(edges)
            edges.append([a, b])
        return edge_ids[a, b]
    seen_faces, normal_sums = set(), [[0.0]*3 for _ in nodes]
    structural = set()
    for i in sorted(surface):
        a, b, c = [node_for_point[v] for v in topology[i]]
        require(len({a, b, c}) == 3, 'Repeated point in a cloth face')
        key = tuple(sorted((a, b, c)))
        require(key not in seen_faces, 'Duplicate/opposite cloth face on the same points; use one simulation surface')
        seen_faces.add(key)
        e0, e1 = edge(a, b), edge(a, c)
        edge(b, c)
        normal = geometry.cross(geometry.sub(nodes[b]['position'], nodes[a]['position']),
                                geometry.sub(nodes[c]['position'], nodes[a]['position']))
        require(dot(normal, normal) > 0, f'Cloth face {i} has zero area')
        for n in (a, b, c):
            normal_sums[n] = [x+y for x, y in zip(normal_sums[n], normal)]
        sim_triangles.append([e0, e1, a, b, c])
        for x, y, opposite in ((a, b, c), (b, c, a), (c, a, b)):
            pair = tuple(sorted((x, y)))
            structural.add(pair)
            opposites[pair].append(opposite)
    require(all(len(v) <= 2 for v in opposites.values()), 'Nonmanifold cloth edge; at most two simulation faces may share it')
    bending = {tuple(sorted(v)) for v in opposites.values() if len(v) == 2 and len(set(v)) == 2} - structural
    simulation = {'nodes': nodes, 'edges': edges, 'normal_edge_prefix': len(edges), 'triangles': sim_triangles,
                  'tangents': [], 'collision_neighbors': [], 'springs': [], 'batch_sizes': [],
                  'structural_batch_count': 0, 'time_step_scale': editing.float32(time_scale)}
    for family, pairs in (('structural', structural), ('bending', bending)):
        for a, b in sorted(pairs):
            length = geometry.squared(geometry.sub(nodes[b]['position'], nodes[a]['position']))
            require(length > 0, 'Coincident connected cloth nodes; remove zero-length edges before generation')
            simulation['springs'].append({'nodes': [a, b], 'family': family, 'weight_a': 0.0, 'weight_b': 0.0,
                                          'rest_length_squared': editing.float32(length*(bend_ratio if family == 'bending' else 1))})
    rows = roles.distances(simulation)
    for spring in simulation['springs']:
        a, b = spring['nodes']
        ma, mb = rows[a]**2, rows[b]**2
        if ma+mb:
            spring['weight_a'] = editing.float32(strength*ma/(ma+mb))
            spring['weight_b'] = editing.float32(-strength*mb/(ma+mb))
            require((ma == 0 or spring['weight_a'] > 0) and (mb == 0 or spring['weight_b'] < 0),
                    'Generated spring correction underflow')
        else:
            spring[roles.STRENGTH] = strength
        require(spring['rest_length_squared'] > 0, 'Spring rest length underflow')
    simulation['batch_sizes'], simulation['structural_batch_count'] = roles.batches(simulation['springs'])
    simulation['collision_neighbors'] = roles.collision_records(simulation, rows, [])
    # Derive UV tangents on all render triangles, including rigid boundary
    # corners, then fit their directions to incident simulation edge bases.
    gradients = defaultdict(lambda: [0.0]*3)
    for i, face in enumerate(faces):
        a, b, c = face['corners']
        u, v = geometry.sub(b['position'], a['position']), geometry.sub(c['position'], a['position'])
        du, dv = geometry.sub(b['uv'], a['uv']), geometry.sub(c['uv'], a['uv'])
        determinant = du[0]*dv[1]-dv[0]*du[1]
        area = math.sqrt(geometry.squared(geometry.cross(u, v)))
        if determinant and area:
            gradient = [(x*dv[1]-y*du[1])/determinant*area for x, y in zip(u, v)]
            for source in ids_for_face[i]:
                gradients[source] = [x+y for x, y in zip(gradients[source], gradient)]
    edge_vectors = [geometry.sub(nodes[b]['position'], nodes[a]['position']) for a, b in edges]
    incident = defaultdict(list)
    for i, (a, b) in enumerate(edges):
        incident[a].append(i)
        incident[b].append(i)
    max_error = 0.0
    for mesh in meshes:
        for source in range(mesh['vertex_start'], mesh['vertex_start']+mesh['vertex_count']):
            point = record_points[source]
            if point not in node_for_point:
                continue
            node = node_for_point[point]
            normal = unit(vertices[source]['source_normal'], f'Render vertex {source} has no normal')
            gradient = gradients[source]
            target = unit([x-dot(gradient, normal)*y for x, y in zip(gradient, normal)],
                          f'Render vertex {source}: no usable UV tangent; fix collapsed UVs')
            best = None
            for rank, e0 in enumerate(incident[node]):
                u = edge_vectors[e0]
                g00 = dot(u, u)
                for e1 in incident[node][rank+1:]:
                    v = edge_vectors[e1]
                    g11, g01 = dot(v, v), dot(u, v)
                    det = g00*g11-g01*g01
                    if det <= 32*(2**-23)*g00*g11:
                        continue
                    c0 = editing.float32((g11*dot(u, target)-g01*dot(v, target))/det)
                    c1 = editing.float32((g00*dot(v, target)-g01*dot(u, target))/det)
                    reconstructed = [c0*x+c1*y for x, y in zip(u, v)]
                    if dot(reconstructed, reconstructed) == 0:
                        continue
                    error = geometry.squared(geometry.sub(reconstructed, target))
                    choice = (error, c0*c0+c1*c1, e0, e1, c0, c1)
                    if best is None or choice < best:
                        best = choice
            require(best is not None, f'Node {node}: cannot construct a noncollinear tangent basis')
            error, _, e0, e1, c0, c1 = best
            max_error = max(max_error, error)
            direction = dot(normal, normal_sums[node])
            require(direction != 0, f'Node {node}: simulation normal cancels or is perpendicular to render normal')
            tangent = len(simulation['tangents'])
            simulation['tangents'].append({'edges': [e0, e1], 'coefficients': [c0, c1]})
            mesh['bindings'].append({'vertex': source, 'node': node, 'node_flags': 0 if direction > 0 else 32768,
                                     'tangent_index': len(nodes)+tangent})
    document = {'format': 'vtmb-cloth', 'version': 1, 'lod': 0, 'coordinate_space': 'reference_model',
                'reference': {'bones': bones, 'vertices': vertices, 'triangles': triangles, 'meshes': meshes},
                'simulation': simulation,
                'vtmb_cloth_authoring': {'version': 1, 'generator': 'selected-ref-topology',
                    'strength': strength, 'bend_rest_ratio': bend_ratio, 'time_step_scale': time_scale,
                    'max_tangent_fit_squared_error': max_error}}
    schema.validate(document)
    return document


def generate(context, settings, reference_path, strength, bend_ratio, time_scale):
    saved, obj, faces, topology = draft(settings, context.scene)
    require(saved.get('operation') != 'APPEND', 'Use Add Cloth Region to preserve the loaded cloth')
    fixed = json.loads(settings.authoring_anchors)
    require(fixed, 'Set Generator Anchors before generation')
    require(set(saved['boundary']) <= set(fixed), 'Rigid/cloth boundary points must be fixed')
    path = Path(reference_path).resolve()
    reference_bytes = path.read_bytes()
    reference = smd.loads(reference_bytes.decode('utf-8-sig'))
    document = build(faces, topology, saved['faces'], fixed, reference, path.name, strength, bend_ratio, time_scale)
    obj_name = obj.name
    rig_name = obj.modifiers[0].object.name
    selected = [o.name for o in context.selected_objects]
    active = context.view_layer.objects.active.name if context.view_layer.objects.active else None
    try:
        with tempfile.TemporaryDirectory(prefix='vtmb-cloth-generation-') as stage:
            pose_proof.prove(context, obj.modifiers[0].object, path, document, stage,
                             verify_reference_geometry=False)
    finally:
        for current in context.view_layer.objects:
            current.select_set(current.name in selected)
        context.view_layer.objects.active = context.view_layer.objects.get(active) if active else None
    # Control imports may remap RNA; verify the captured state again by name.
    settings = context.scene.vtmb_cloth
    saved, obj, _, _ = draft(settings, context.scene)
    require(path.read_bytes() == reference_bytes, 'The bind-pose REF SMD changed during generation; try again')
    import bpy
    backup = obj.data.copy()
    payload = proof = None
    old_state = (settings.source_path, settings.source_hash, settings.edit_log, settings.node, settings.spring)
    try:
        # Explicit authoring projection: retain chosen canonical corner normals,
        # then require the ordinary exact attachment/fresh-import identity proof.
        normals = [None]*len(obj.data.loops)
        for polygon, face in zip(obj.data.polygons, faces):
            for loop, corner in zip(polygon.loop_indices, face['corners']):
                normals[loop] = corner['normal']
        obj.data.normals_split_custom_set(normals)
        obj.data.update()
        attachment.attach(settings, context.scene, document, [obj])
        payload = bpy.data.texts.new('VTMB Cloth: generated setup')
        payload.write(schema.dumps(document))
        proof = bpy.data.texts.new('VTMB Cloth: generated reference proof')
        proof.write(json.dumps({'version': 1, 'reference_path': str(path),
                                'sha256': hashlib.sha256(reference_bytes).hexdigest(),
                                'rig': rig_name, 'rest': pose_proof.signature(bpy.data.objects[rig_name])}, indent=2))
        settings.payload, settings.generated_proof = payload, proof
        settings.source_path, settings.source_hash = str(path), hashlib.sha256(reference_bytes).hexdigest()
        settings.edit_log = json.dumps([{'type': 'generate_cloth', 'cloth_faces': len(saved['faces']),
            'fixed_points': fixed, 'parameters': document['vtmb_cloth_authoring']}], allow_nan=False)
        settings.node = settings.spring = 0
        attachment.validate_owned(settings, context.scene, document)
    except Exception:
        attachment.detach(settings)
        settings.payload = None
        obj = context.scene.objects.get(obj_name)
        if obj is not None:
            failed = obj.data
            obj.data = backup
            if failed.users == 0:
                bpy.data.meshes.remove(failed)
        settings.source_path, settings.source_hash, settings.edit_log, settings.node, settings.spring = old_state
        for text in (payload, proof):
            if text is not None:
                bpy.data.texts.remove(text)
        raise
    bpy.data.meshes.remove(backup)
    settings.authoring_draft = None
    settings.authoring_anchors = '[]'
    settings.binding_status = f'Generated cloth: {len(document["simulation"]["nodes"])} nodes, {len(saved["faces"])} selected faces'
    return document
