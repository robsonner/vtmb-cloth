"""Conservative triangle/corner matching, independent of Blender and Source Tools."""
import struct
import json
from collections import defaultdict
from .schema import require


def key(corner):
    # Compare the stored Blender float32 representation of emitted SMD values.
    return struct.pack('<5f', *(corner['position'] + corner['uv']))


def skin_compatible(a, b):
    def skin(c):
        weights = defaultdict(float)
        for name, weight in c['skinning']:
            if weight > 0:
                weights[name] += weight
        total = sum(weights.values())
        require(total > 0, 'Missing real skin weights')
        return {name: w/total for name, w in weights.items()}
    sa, sb = skin(a), skin(b)
    return sa.keys() == sb.keys() and all(abs(w-sb[n]) <= 1e-7 for n, w in sa.items())


def compatible(a, b):
    return (struct.pack('<3f', *a['normal']) == struct.pack('<3f', *b['normal'])
            and skin_compatible(a, b))


def candidate_options(d, faces, corner_test=skin_compatible):
    """Scan face candidates; empty entries permit finding a protected subset."""
    names = {b['id']: b['name'] for b in d['reference']['bones']}
    vertices = {}
    for v in d['reference']['vertices']:
        if v['exported'] is not None:
            e = dict(v['exported'])
            e['skinning'] = [(names[b], w) for b, w in e['skinning']]
            vertices[v['id']] = e
    buckets = defaultdict(list)
    for index, t in enumerate(d['reference']['triangles']):
        buckets[(t['material'], tuple(sorted(key(vertices[v]) for v in t['vertices'])))].append(index)
    result = []
    for face_index, face in enumerate(faces):
        bucket = buckets.get((face['material'], tuple(sorted(key(c) for c in face['corners']))), [])
        choices = []
        for index in bucket:
            ids = d['reference']['triangles'][index]['vertices']
            # An unchanged REF may rotate its starting corner, but must retain
            # face winding. Reversing a triangle selects its opposite surface
            # on double-sided geometry and is not an equivalent source edit.
            for order in ((0, 1, 2), (1, 2, 0), (2, 0, 1)):
                mapped = tuple(ids[i] for i in order)
                if all(key(c) == key(vertices[v]) and corner_test(c, vertices[v]) for c, v in zip(face['corners'], mapped)):
                    choices.append((index, mapped))
        result.append(choices)
    return result


def candidates(d, faces, corner_test=skin_compatible):
    """Return every compatible source face/corner permutation; no identity guess."""
    result = candidate_options(d, faces, corner_test)
    for face_index, choices in enumerate(result):
        require(choices, f'Face {face_index}: no complete material/position/UV/normal/skin match')
    require(len(faces) == len(d['reference']['triangles']), 'Target set omits/duplicates source triangles')
    return result


def _match(d, faces, corner_test):
    used, result = set(), []
    for face_index, choices in enumerate(candidates(d, faces, corner_test)):
        choices = [(i, m) for i, m in choices if i not in used]
        require(choices, f'Face {face_index}: source triangle already consumed')
        require(len({mapping for _, mapping in choices}) == 1,
                f'Face {face_index}: ambiguous original vertex identities; attachment blocked')
        index, mapped = choices[0]
        used.add(index)
        result.append(mapped)
    require(len(used) == len(d['reference']['triangles']), 'Target set omits source triangles')
    return result


def match(d, faces):
    """Exact float32 corner-normal matching for already preserved source data."""
    return _match(d, faces, compatible)


def provisional_mapping(d, faces):
    """Geometry/UV/skin identity proposal, NOT a successful attachment.

    The caller must prove source normals project to current Blender corner
    normals on the actual topology. Reject ambiguous identities even when
    their geometry is indistinguishable; never guess from face order.
    """
    return _match(d, faces, skin_compatible)


def retained_signatures(document):
    """Include every identity-dependent field except raw source-normal provenance."""
    bindings = {b['vertex']: {k:v for k,v in b.items() if k != 'vertex'}
                for m in document['reference']['meshes'] for b in m['bindings']}
    refs = defaultdict(list)
    for node in document['simulation']['nodes']:
        refs[node['reference_vertex']].append(node['id'])
    signatures = {}
    for vertex in document['reference']['vertices']:
        v = vertex['id']
        signatures[v] = json.dumps([{k:value for k,value in vertex.items() if k not in ('id', 'source_normal')},
                                   bindings.get(v), refs[v]], sort_keys=True, separators=(',', ':'))
    return signatures


def resolve_candidates(document, options, signatures=None):
    """Prove alias equivalence, then find a one-to-one source-face assignment.

    Raw source_normal is provenance: v1 rendering uses exported.normal. Keep
    both original records in the canonical payload; never merge their IDs.
    Aliases are interchangeable only when all other fields (including unknown
    ones), bindings and node-reference consumers are identical.
    """
    signatures = retained_signatures(document) if signatures is None else signatures
    for face, choices in enumerate(options):
        require(choices, f'Face {face}: source normal projection disagrees')
        require(len({tuple(signatures[v] for v in ids) for _,ids in choices}) == 1,
                f'Face {face}: incompatible retained source identities; attachment blocked')
    assigned, chosen = {}, {}

    def assign(face, seen):
        for source, ids in options[face]:
            if source in seen:
                continue
            seen.add(source)
            if source not in assigned or assign(assigned[source], seen):
                assigned[source] = face
                chosen[face] = ids
                return True
        return False

    for face in range(len(options)):
        require(assign(face, set()), f'Face {face}: no one-to-one source triangle assignment')
    require(len(assigned) == len(document['reference']['triangles']), 'Incomplete source triangle coverage')
    return [chosen[i] for i in range(len(options))]
