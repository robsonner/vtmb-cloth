"""Owned, explicitly validated corner provenance on existing REF meshes."""
import hashlib
import json
import struct
import uuid
from collections import Counter
from .schema import require
from . import scene_data, matching

ATTRIBUTES = {'vtmb_source_vertex': 'INT', 'vtmb_node': 'INT', 'vtmb_source_normal': 'FLOAT_VECTOR'}
OWNER = 'vtmb_cloth_owner'


def fingerprint(document, faces):
    raw = json.dumps([document, faces], sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def targets(settings, scene):
    objects = [item.object for item in settings.targets]
    require(objects and all(obj is not None and scene.objects.get(obj.name) == obj for obj in objects),
            'Attached REF object is missing from this scene; detach and match again')
    require(len({obj.as_pointer() for obj in objects}) == len(objects), 'Duplicate target ownership')
    token = settings.binding_token
    require(token, 'No owned REF attachment')
    for obj in objects:
        require(obj.get(OWNER) == token and obj.data.get(OWNER) == token, 'Stale/copied target ownership')
    # A copied object/mesh must not silently inherit the original identity.
    import bpy
    require(not any(o not in objects and o.get(OWNER) == token for o in bpy.data.objects),
            'Copied attachment detected; remove copies or detach and reattach the original REF')
    return objects


def detach(settings):
    settings.geometry_session = None
    settings.geometry_origin = None
    settings.generated_proof = None
    token = settings.binding_token
    for item in settings.targets:
        obj = item.object
        if obj is None or obj.get(OWNER) != token:
            continue
        if obj.type == 'MESH' and obj.data.users == 1 and obj.data.get(OWNER) == token:
            for name in ATTRIBUTES:
                attribute = obj.data.attributes.get(name)
                if attribute is not None:
                    obj.data.attributes.remove(attribute)
            del obj.data[OWNER]
        del obj[OWNER]
    settings.targets.clear()
    settings.binding_token = settings.binding_digest = ''
    settings.binding_status = 'No REF attachment'


def attach(settings, scene, document, objects):
    objects = tuple(objects)
    if settings.targets:
        require(objects == tuple(targets(settings, scene)), 'Detach the previous REF set before choosing another')
    faces, owners = scene_data.read_faces(objects)
    mapped = scene_data.match_projected(document, objects, faces, owners)
    covered = {vertex for ids in mapped for vertex in ids}
    required = {b['vertex'] for mesh in document['reference']['meshes'] for b in mesh['bindings']}
    required.update(n['reference_vertex'] for n in document['simulation']['nodes'])
    require(required <= covered, 'Required binding/node reference absent from matched REF corners')
    digest = fingerprint(document, faces)
    # Preflight every target before writing any metadata or attributes.
    for obj in objects:
        owner = obj.get(OWNER)
        require(owner is None or owner == settings.binding_token, f'{obj.name}: already owned by another attachment')
        mesh_owner = obj.data.get(OWNER)
        require(mesh_owner is None or mesh_owner == settings.binding_token, f'{obj.name}: mesh is owned by another attachment')
        for name, kind in ATTRIBUTES.items():
            attribute = obj.data.attributes.get(name)
            require(attribute is None or (owner == settings.binding_token and attribute.domain == 'CORNER' and attribute.data_type == kind),
                    f'{obj.name}: stale or incompatible {name}; detach its owner first')
    token = settings.binding_token or uuid.uuid4().hex
    bindings = {b['vertex']: b['node'] for m in document['reference']['meshes'] for b in m['bindings']}
    # Snapshot existing values for rollback if Blender rejects a write.
    backups, created = [], []
    try:
        for obj in objects:
            for name, kind in ATTRIBUTES.items():
                attribute = obj.data.attributes.get(name)
                field = 'vector' if kind == 'FLOAT_VECTOR' else 'value'
                if attribute is None:
                    obj.data.attributes.new(name, kind, 'CORNER')
                    created.append((obj, name))
                else:
                    backups.append((obj, name, field, [tuple(v.vector) if field == 'vector' else v.value for v in attribute.data]))
        for (obj, loops), ids in zip(owners, mapped):
            for loop, source in zip(loops, ids):
                obj.data.attributes['vtmb_source_vertex'].data[loop].value = source
                obj.data.attributes['vtmb_node'].data[loop].value = bindings.get(source, -1)
                obj.data.attributes['vtmb_source_normal'].data[loop].vector = document['reference']['vertices'][source]['exported']['normal']
    except Exception:
        for obj, name, field, values in backups:
            for item, value in zip(obj.data.attributes[name].data, values):
                setattr(item, field, value)
        for obj, name in reversed(created):
            attribute = obj.data.attributes.get(name)
            if attribute is not None:
                obj.data.attributes.remove(attribute)
        raise
    settings.targets.clear()
    for obj in objects:
        obj[OWNER] = obj.data[OWNER] = token
        settings.targets.add().object = obj
    settings.binding_token, settings.binding_digest = token, digest
    settings.binding_status = f'Last check passed: {len(mapped)} source faces'
    return len(mapped)


def validate(settings, scene, document):
    objects = targets(settings, scene)
    faces, owners = scene_data.read_faces(objects)
    require(fingerprint(document, faces) == settings.binding_digest,
            'REF geometry, normals, UVs, weights or payload changed; detach and match again')
    bindings = {b['vertex']: b['node'] for m in document['reference']['meshes'] for b in m['bindings']}
    for obj in objects:
        for name, kind in ATTRIBUTES.items():
            attribute = obj.data.attributes.get(name)
            require(attribute is not None and attribute.domain == 'CORNER' and attribute.data_type == kind,
                    f'{obj.name}: missing/incompatible corner provenance')
    mapped = scene_data.match_projected(document, objects, faces, owners)
    for (obj, loops), ids in zip(owners, mapped):
        for loop, source in zip(loops, ids):
            attributes = obj.data.attributes
            require(attributes['vtmb_source_vertex'].data[loop].value == source and
                    attributes['vtmb_node'].data[loop].value == bindings.get(source, -1), 'Edited/stale corner identity')
            require(struct.pack('<3f', *attributes['vtmb_source_normal'].data[loop].vector) ==
                    struct.pack('<3f', *document['reference']['vertices'][source]['exported']['normal']),
                    'Edited/stale preserved source normal')
    return objects


def validate_owned(settings, scene, document):
    """Verify an unchanged owned attachment without discovering identities again.

    The document/face digest proves this is the already accepted geometry and
    payload. Check stored identities against complete oriented source triangles,
    positions, UVs, skinning, bindings and one exact normal projection. Initial
    attachment still uses the full ambiguity search.
    """
    objects = targets(settings, scene)
    faces, owners = scene_data.read_faces(objects)
    require(fingerprint(document, faces) == settings.binding_digest,
            'REF geometry, normals, UVs, weights or payload changed; restore the validated REF')
    vertices = document['reference']['vertices']
    names = {b['id']: b['name'] for b in document['reference']['bones']}
    bindings = {b['vertex']: b['node'] for m in document['reference']['meshes'] for b in m['bindings']}
    for obj in objects:
        for name, kind in ATTRIBUTES.items():
            attribute = obj.data.attributes.get(name)
            require(attribute is not None and attribute.domain == 'CORNER' and attribute.data_type == kind,
                    f'{obj.name}: missing/incompatible corner provenance')
    def oriented(ids):
        ids = tuple(ids)
        return min(ids[i:]+ids[:i] for i in range(3))
    expected = Counter((t['material'], oriented(t['vertices'])) for t in document['reference']['triangles'])
    observed, mapped = Counter(), []
    for (obj, loops), face in zip(owners, faces):
        attributes = obj.data.attributes
        ids = tuple(attributes['vtmb_source_vertex'].data[l].value for l in loops)
        for loop, source, corner in zip(loops, ids, face['corners']):
            require(0 <= source < len(vertices) and vertices[source]['exported'] is not None,
                    'Edited/stale source corner identity')
            original = vertices[source]['exported']
            require(attributes['vtmb_node'].data[loop].value == bindings.get(source, -1),
                    'Edited/stale node identity')
            require(struct.pack('<3f', *attributes['vtmb_source_normal'].data[loop].vector) ==
                    struct.pack('<3f', *original['normal']), 'Edited/stale preserved source normal')
            source_corner = dict(original, skinning=[(names[b], w) for b, w in original['skinning']])
            require(matching.key(corner) == matching.key(source_corner) and
                    matching.skin_compatible(corner, source_corner), 'Stored corner identity disagrees with REF')
        observed[(face['material'], oriented(ids))] += 1
        mapped.append(ids)
    require(observed == expected, 'Stored provenance omits, duplicates or reverses source triangles')
    projected = scene_data._project(objects, owners, mapped, document)
    direct_normals = document.get('vtmb_cloth_reference_normal_policy') == 'current-decoded-corners-v1'
    for (obj, loops), ids in zip(owners, mapped):
        require(all(projected[obj.as_pointer()][loop] == struct.pack('<3f', *obj.data.corner_normals[loop].vector)
                    or (direct_normals and struct.pack('<3f', *vertices[source]['exported']['normal']) ==
                        struct.pack('<3f', *obj.data.corner_normals[loop].vector))
                    for loop, source in zip(loops, ids)),
                f'{obj.name}: stored identities fail exact source-normal projection')
    return objects, faces
