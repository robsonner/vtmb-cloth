"""Dependency-free, preserving VCLOTH v1 reader. No Blender imports."""
import json
import math
import struct
from pathlib import Path


def require(ok, message):
    if not ok:
        raise ValueError(message)


def integer(value, maximum, minimum=0):
    require(type(value) is int and minimum <= value <= maximum,
            f"Invalid integer/index: {value!r}; expected {minimum}..{maximum}")
    return value


def scalar(value):
    require(type(value) in (int, float) and math.isfinite(value), "Nonfinite/non-numeric value")
    try:
        require(math.isfinite(struct.unpack('<f', struct.pack('<f', value))[0]), "Float32 overflow")
    except (OverflowError, struct.error) as exc:
        raise ValueError("Float32 overflow") from exc
    return value


def vector(value, width):
    require(isinstance(value, list) and len(value) == width, f"Expected vector width {width}")
    for item in value:
        scalar(item)


def indices(value, maxima):
    require(isinstance(value, list) and len(value) == len(maxima), "Wrong index record width")
    for item, maximum in zip(value, maxima):
        integer(item, maximum)


def validate(d):
    """Check native v1 table invariants without rewriting any payload fields."""
    require(d['format'] == 'vtmb-cloth', "Unsupported format")
    integer(d['version'], 1, 1)
    integer(d['lod'], 0)
    require(d['coordinate_space'] == 'reference_model', "Unsupported coordinates")
    r, s = d['reference'], d['simulation']
    bones, vertices, nodes = r['bones'], r['vertices'], s['nodes']
    nv, nn = len(vertices), len(nodes)
    require(nv > 0 and 0 < nn <= 32768, "Invalid vertex/node count")
    for table in (bones, vertices, nodes):
        for i, entry in enumerate(table):
            require(type(entry['id']) is int and entry['id'] == i, "IDs must be dense and ordered")
    names = set()
    for b in bones:
        require(isinstance(b['name'], str) and b['name'] not in names, "Invalid/duplicate bone name")
        names.add(b['name'])
        integer(b['parent'], len(bones)-1, -1)
        require(b['parent'] != b['id'], "Self-parented bone")
        vector(b['position'], 3)
        vector(b['rotation_xyzw'], 4)
    for v in vertices:
        for field, width in (('source_position', 3), ('source_normal', 3), ('source_uv', 2)):
            vector(v[field], width)
        for link in v['skinning']:
            integer(link['bone'], len(bones)-1)
            integer(link['weight_byte'], 255)
        e = v['exported']
        if e is not None:
            for field, width in (('position', 3), ('normal', 3), ('uv', 2)):
                vector(e[field], width)
            integer(e['parent_bone'], len(bones)-1)
            total = 0
            for link in e['skinning']:
                require(isinstance(link, list) and len(link) == 2, "Invalid skin link")
                integer(link[0], len(bones)-1)
                require(scalar(link[1]) >= 0, "Negative skin weight")
                total += link[1]
            require(math.isfinite(total) and total > 0, "Missing exported weights")
    used = set()
    for t in r['triangles']:
        require(isinstance(t['material'], str), "Invalid material")
        indices(t['vertices'], [nv-1]*3)
        require(all(vertices[i]['exported'] is not None for i in t['vertices']), "Unexported face corner")
        used.update(t['vertices'])
    fixed = 0
    for i, n in enumerate(nodes):
        require(type(n['fixed']) is bool, "Invalid fixed flag")
        integer(n['reference_vertex'], nv-1)
        require(n['reference_vertex'] in used, "Node reference absent from REF triangles")
        vector(n['position'], 3)
        require(struct.pack('<3f', *n['position']) == struct.pack('<3f', *vertices[n['reference_vertex']]['source_position']), "Node/reference position mismatch")
        if n['fixed']:
            require(fixed == i, "Fixed nodes must precede free nodes")
            fixed += 1
    require(0 < fixed < nn, "Invalid fixed/free partition")
    require(scalar(s['time_step_scale']) > 0, "Invalid time step scale")
    structural = 0
    require(len(s['springs']) > 0, "No springs")
    for i, spring in enumerate(s['springs']):
        indices(spring['nodes'], [nn-1]*2)
        require(spring['nodes'][0] != spring['nodes'][1], "Self spring")
        require(scalar(spring['weight_a']) >= 0 and scalar(spring['weight_b']) <= 0 and scalar(spring['rest_length_squared']) > 0, "Invalid signed spring factors/rest length")
        require(spring['family'] in ('structural', 'bending'), "Unknown spring family")
        if spring['family'] == 'structural':
            require(structural == i, "Structural springs must precede bending springs")
            structural += 1
    ne, nt = len(s['edges']), len(s['tangents'])
    require(0 < ne <= 65536 and nn + nt <= 65536, "Table exceeds native index range")
    for e in s['edges']:
        indices(e, [nn-1]*2)
    integer(s['normal_edge_prefix'], ne)
    for t in s['triangles']:
        indices(t, [ne-1]*2 + [nn-1]*3)
    for t in s['tangents']:
        indices(t['edges'], [ne-1]*2)
        vector(t['coefficients'], 2)
    require(len(s['collision_neighbors']) <= nn-fixed, "Too many collision records")
    for record in s['collision_neighbors']:
        indices(record, [nn-1]*3)
    for size in s['batch_sizes']:
        integer(size, 4, 1)
    boundary = integer(s['structural_batch_count'], len(s['batch_sizes']))
    require(sum(s['batch_sizes'][:boundary]) == structural and sum(s['batch_sizes']) == len(s['springs']), "Batch coverage mismatch")
    assigned = set()
    for mesh in r['meshes']:
        start = integer(mesh['vertex_start'], nv)
        count = integer(mesh['vertex_count'], nv-start)
        for binding in mesh['bindings']:
            v = integer(binding['vertex'], start+count-1, start)
            require(v not in assigned, "Duplicate render binding")
            assigned.add(v)
            integer(binding['node'], nn-1)
            require(type(binding['node_flags']) is int and binding['node_flags'] in (0, 32768), "Unsupported node flags")
            integer(binding['tangent_index'], nn+nt-1, nn)
    return {'nodes': nn, 'fixed': fixed, 'free': nn-fixed, 'springs': len(s['springs']),
            'bindings': len(assigned), 'triangles': len(r['triangles'])}


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def loads(text):
    try:
        d = json.loads(text, object_pairs_hook=_pairs)
        if isinstance(d, dict):
            d.pop('reference_smd', None)  # Obsolete filename metadata is not a binding.
            d.pop('source_model', None)
        # Include unknown fields in finite-number validation and retain them.
        def walk(value):
            if isinstance(value, dict):
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
            elif type(value) is float:
                require(math.isfinite(value), "Nonfinite JSON number")
        walk(d)
        validate(d)
        return d
    except (KeyError, TypeError, IndexError, RecursionError) as exc:
        raise ValueError(f"Malformed VCLOTH: {exc}") from exc


def read(path):
    path = Path(path)
    require(path.stat().st_size <= 64*1024*1024, "VCLOTH exceeds 64 MiB")
    return loads(path.read_text(encoding='utf-8'))


def dumps(d):
    validate(d)
    payload = dict(d)
    payload.pop('reference_smd', None)
    payload.pop('source_model', None)
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + '\n'
