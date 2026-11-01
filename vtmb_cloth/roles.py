"""Experimental fixed/free authoring on an existing cloth graph.

Distance-based mobility and collision neighborhoods are explicit project
policies, not recovered stock authoring rules. No mesh or springs are added.
"""
import copy
import json
from array import array
from collections import deque
from . import attachment, editing, schema
from .schema import require

STRENGTH = 'vtmb_cloth_inactive_strength_v1'


def selected_nodes(document, objects):
    """Include render bindings and explicit skinning references, never proximity."""
    sources, nodes = set(), set()
    for obj in objects:
        if not obj.select_get():
            continue
        mesh = obj.data
        source = mesh.attributes['vtmb_source_vertex'].data
        binding = mesh.attributes['vtmb_node'].data
        for loop in mesh.loops:
            if mesh.vertices[loop.vertex_index].select:
                sources.add(source[loop.index].value)
                if binding[loop.index].value >= 0:
                    nodes.add(binding[loop.index].value)
    nodes.update(n['id'] for n in document['simulation']['nodes'] if n['reference_vertex'] in sources)
    require(nodes, 'Select cloth vertices in Edit Mode, then return to Object Mode with the REF selected')
    return sorted(nodes)


def distances(simulation):
    nodes = simulation['nodes']
    graph = [set() for _ in nodes]
    for spring in simulation['springs']:
        if spring['family'] == 'structural':
            a, b = spring['nodes']
            graph[a].add(b)
            graph[b].add(a)
    rows = [0 if n['fixed'] else -1 for n in nodes]
    queue = deque(i for i, row in enumerate(rows) if row == 0)
    while queue:
        a = queue.popleft()
        for b in sorted(graph[a]):
            if rows[b] < 0:
                rows[b] = rows[a]+1
                queue.append(b)
    missing = next((i for i, row in enumerate(rows) if row < 0), None)
    require(missing is None,
            f'Free node {missing} has no structural-spring path to a fixed node; keep an anchor in every connected region')
    return rows


def batches(springs):
    """Pack contiguous disjoint springs, retaining family and spring order."""
    sizes, used, count = [], set(), 0
    previous_family, structural_batches = None, 0
    for spring in springs:
        family = spring['family']
        endpoints = set(spring['nodes'])
        if count and (family != previous_family or count == 4 or used.intersection(endpoints)):
            sizes.append(count)
            structural_batches += previous_family == 'structural'
            used, count = set(), 0
        used.update(endpoints)
        count += 1
        previous_family = family
    if count:
        sizes.append(count)
        structural_batches += previous_family == 'structural'
    return sizes, structural_batches


def collision_records(simulation, rows, old_records):
    """At most one directed neighborhood per free n1; n1/n2 must stay free.

    Prefer valid retained records, then oriented simulation triangles with an
    upstream n0. This is not an assertion of per-free-node engine ownership.
    """
    nodes = simulation['nodes']
    choices = {}
    for index, record in enumerate(old_records):
        a, b, c = record
        if len(set(record)) == 3 and not nodes[b]['fixed'] and not nodes[c]['fixed']:
            choices.setdefault(b, []).append(((0, index), record))
    for index, triangle in enumerate(simulation['triangles']):
        ids = triangle[2:]
        if len(set(ids)) != 3:
            continue
        for shift in range(3):
            a, b, c = ids[shift:]+ids[:shift]
            if nodes[b]['fixed'] or nodes[c]['fixed']:
                continue
            p, q, r = (nodes[v]['position'] for v in (a, b, c))
            u, v = [x-y for x, y in zip(q, p)], [x-y for x, y in zip(r, p)]
            area = sum(x*x for x in (u[1]*v[2]-u[2]*v[1], u[2]*v[0]-u[0]*v[2], u[0]*v[1]-u[1]*v[0]))
            if area > 0:
                choices.setdefault(b, []).append(((1, rows[a], -area, index, shift), [a, b, c]))
    records = [min(choices[b], key=lambda item: item[0])[1] for b in sorted(choices)]
    return records


def set_roles(document, node_ids, fixed, activation_strength=2.0):
    schema.validate(document)
    require(type(fixed) is bool, 'Invalid target role')
    schema.scalar(activation_strength)
    require(activation_strength > 0, 'Activation strength must be positive')
    original = document['simulation']
    selected = sorted(set(node_ids))
    require(selected, 'No cloth nodes selected')
    for node in selected:
        schema.integer(node, len(original['nodes'])-1)
    changed = [i for i in selected if original['nodes'][i]['fixed'] != fixed]
    result = copy.deepcopy(document)
    if not changed:
        return result, None
    simulation = result['simulation']
    for i in changed:
        simulation['nodes'][i]['fixed'] = fixed
    fixed_count = sum(n['fixed'] for n in simulation['nodes'])
    require(0 < fixed_count < len(simulation['nodes']), 'Keep at least one fixed and one free cloth node')
    # Stable partition: preserve identities within each role, remap every node
    # consumer, and retain edge/tangent indices and the normal cache boundary.
    order = sorted(range(len(simulation['nodes'])), key=lambda i: not simulation['nodes'][i]['fixed'])
    remap = [0]*len(order)
    for new, old in enumerate(order):
        remap[old] = new
    simulation['nodes'] = [simulation['nodes'][i] for i in order]
    for i, node in enumerate(simulation['nodes']):
        node['id'] = i
    for mesh in result['reference']['meshes']:
        for binding in mesh['bindings']:
            binding['node'] = remap[binding['node']]
    for spring in simulation['springs']:
        spring['nodes'] = [remap[n] for n in spring['nodes']]
    simulation['edges'] = [[remap[n] for n in edge] for edge in simulation['edges']]
    simulation['triangles'] = [t[:2]+[remap[n] for n in t[2:]] for t in simulation['triangles']]
    old_records = [[remap[n] for n in record] for record in simulation['collision_neighbors']]
    rows = distances(simulation)
    adjusted, activated, factor_changes = [], [], []
    for i, spring in enumerate(simulation['springs']):
        a, b = spring['nodes']
        total = spring['weight_a']-spring['weight_b']
        if STRENGTH in spring:
            schema.scalar(spring[STRENGTH])
            require(spring[STRENGTH] >= 0, 'Invalid retained inactive spring strength')
        if total == 0:
            total = spring.get(STRENGTH, 0)
        ma, mb = rows[a]**2, rows[b]**2
        if ma+mb == 0:
            # Keep fixed-only springs in the graph so releasing nodes later
            # can restore their original total correction magnitude.
            spring[STRENGTH] = total
            wa = wb = 0.0
        else:
            if total == 0:
                total = activation_strength
                activated.append(i)
            wa = editing.float32(total*ma/(ma+mb))
            wb = editing.float32(-total*mb/(ma+mb))
            require((ma == 0 or wa > 0) and (mb == 0 or wb < 0),
                    f'Spring {i}: correction magnitude underflow')
        if (wa, wb) != (spring['weight_a'], spring['weight_b']):
            adjusted.append(i)
            factor_changes.append({'spring': i, 'before': [spring['weight_a'], spring['weight_b']],
                                   'after': [wa, wb]})
        spring['weight_a'], spring['weight_b'] = wa, wb
    simulation['batch_sizes'], simulation['structural_batch_count'] = batches(simulation['springs'])
    simulation['collision_neighbors'] = collision_records(simulation, rows, old_records)
    schema.validate(result)
    # Validate generated safety properties beyond the preserving v1 parser.
    offset = 0
    for size in simulation['batch_sizes']:
        endpoints = [n for s in simulation['springs'][offset:offset+size] for n in s['nodes']]
        require(len(endpoints) == len(set(endpoints)), 'Generated SIMD batch has shared endpoints')
        offset += size
    for a, b, c in simulation['collision_neighbors']:
        require(b >= fixed_count and c >= fixed_count and len({a, b, c}) == 3,
                'Generated collision record could modify a fixed node')
    return result, {
        'type': 'node_roles', 'role': 'FIXED' if fixed else 'FREE',
        'changed_old_nodes': changed, 'old_to_new_nodes': remap,
        'fixed_before': sum(n['fixed'] for n in original['nodes']), 'fixed_after': fixed_count,
        'adjusted_springs': adjusted, 'activated_zero_strength_springs': activated,
        'spring_factor_changes': factor_changes,
        'activation_strength': activation_strength,
        'mobility_policy': 'squared-structural-distance-preserve-total-strength',
        'batch_policy': 'contiguous-disjoint-endpoints-preserve-spring-order',
        'collision_policy': 'one-directed-record-per-free-n1-prefer-retained-then-oriented-surface',
        'collision_records_before': len(old_records),
        'collision_records_after': len(simulation['collision_neighbors']),
    }


def apply(settings, scene, document, node_ids, fixed, activation_strength):
    require(settings.geometry_session is None, 'Finish or restore the active geometry edit first')
    objects, faces = attachment.validate_owned(settings, scene, document)
    updated, entry = set_roles(document, node_ids, fixed, activation_strength)
    if entry is None:
        return None
    raw = schema.dumps(updated)
    history = json.loads(settings.edit_log)
    require(isinstance(history, list), 'Invalid cloth edit history')
    history.append(entry)
    log = json.dumps(history, allow_nan=False)
    digest = attachment.fingerprint(updated, faces)
    bindings = {b['vertex']: b['node'] for m in updated['reference']['meshes'] for b in m['bindings']}
    backups, replacements = [], []
    for obj in objects:
        data = obj.data.attributes['vtmb_node'].data
        values = array('i', [0])*len(data)
        data.foreach_get('value', values)
        backups.append((obj, values))
        replacements.append(array('i', (bindings.get(v.value, -1)
                                       for v in obj.data.attributes['vtmb_source_vertex'].data)))
    import bpy
    old_payload, old_digest, old_log, old_node = settings.payload, settings.binding_digest, settings.edit_log, settings.node
    payload = None
    try:
        payload = bpy.data.texts.new('VTMB Cloth: node roles')
        payload.write(raw)
        for (obj, _), values in zip(backups, replacements):
            obj.data.attributes['vtmb_node'].data.foreach_set('value', values)
            obj.data.update()
        settings.payload, settings.binding_digest, settings.edit_log = payload, digest, log
        if settings.node < len(entry['old_to_new_nodes']):
            settings.node = entry['old_to_new_nodes'][settings.node]
        attachment.validate_owned(settings, scene, updated)
    except Exception:
        for obj, values in backups:
            obj.data.attributes['vtmb_node'].data.foreach_set('value', values)
            obj.data.update()
        settings.payload, settings.binding_digest, settings.edit_log, settings.node = old_payload, old_digest, old_log, old_node
        if payload is not None:
            bpy.data.texts.remove(payload)
        raise
    settings.binding_status = f'{len(entry["changed_old_nodes"])} nodes set {entry["role"]}; fixed prefix and dependent tables updated'
    return entry
