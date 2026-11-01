"""Read original mesh corners, never evaluated animation geometry."""
from .schema import require
from . import matching
import struct


def _normal_bytes(vector):
    return struct.pack('<3f', *vector)


def _project(objects, owners, mapped, document, allow_unmapped=False, protected_vertices=None):
    """Project source normals through this Blender version on unlinked data.

    Disposable mesh data is never linked to an object or scene, and is removed
    in finally. The artist's geometry, shading, weights and rig are untouched.
    """
    import bpy
    arrays = {o.as_pointer(): ([list(n.vector) for n in o.data.corner_normals] if allow_unmapped
                              else [None] * len(o.data.loops)) for o in objects}
    for (obj, loops), ids in zip(owners, mapped):
        for loop, source in zip(loops, ids):
            if protected_vertices is None or source in protected_vertices:
                arrays[obj.as_pointer()][loop] = document['reference']['vertices'][source]['exported']['normal']
    projected = {}
    for obj in objects:
        scratch = obj.data.copy()
        try:
            scratch.normals_split_custom_set(arrays[obj.as_pointer()])
            scratch.update()
            projected[obj.as_pointer()] = [_normal_bytes(n.vector) for n in scratch.corner_normals]
        finally:
            bpy.data.meshes.remove(scratch)
    return projected


def match_projected(document, objects, faces, owners, allow_unmapped=False,
                    protected_vertices=None, geometry_first=False):
    """Match source identities with exact normal evidence by default.

    Candidate ranks are projected in parallel over the complete object set.
    Surviving identities must be unambiguous and cover every source triangle;
    the final combined assignment is projected and verified again. Complex
    ambiguous cases fail conservatively rather than attempting nearest matching.
    Scoped append authoring may accept unique semantic geometry identities
    without historical normal evidence. Its newly authored current-normal
    snapshot accepts exact decoded values or their exact import projection.
    """
    options = matching.candidates(document, faces)
    require(protected_vertices is None or allow_unmapped, 'Scoped normal proof requires explicit subset mode')
    require(not geometry_first or protected_vertices is not None, 'Geometry-first matching requires scoped authoring')
    signatures = None
    if protected_vertices is not None:
        signatures = matching.retained_signatures(document)
        signatures = {v: ('protected:'+s if v in protected_vertices else 'rigid-context')
                      for v, s in signatures.items()}
    unique_geometry = [geometry_first and len({tuple(signatures[v] for v in ids)
                                               for _, ids in choices}) == 1 for choices in options]
    direct_normals = document.get('vtmb_cloth_reference_normal_policy') == 'current-decoded-corners-v1'
    def project(mapped):
        if protected_vertices is not None:
            return _project(objects, owners, mapped, document, allow_unmapped=True,
                            protected_vertices=protected_vertices)
        if allow_unmapped:
            return _project(objects, owners, mapped, document, allow_unmapped=True)
        return _project(objects, owners, mapped, document)
    maximum = max(map(len, options), default=0)
    require(maximum <= 64, 'Too many identity candidates; attachment blocked')
    if geometry_first and all(unique_geometry):
        return matching.resolve_candidates(document, options, signatures=signatures)
    actual = {o.as_pointer(): [_normal_bytes(n.vector) for n in o.data.corner_normals] for o in objects}
    def agrees(i, projected, ids):
        if unique_geometry[i]:
            return True
        obj, loops = owners[i]
        return all(projected[obj.as_pointer()][loop] == actual[obj.as_pointer()][loop] or
                   (direct_normals and _normal_bytes(document['reference']['vertices'][source]['exported']['normal'])
                    == actual[obj.as_pointer()][loop])
                   for loop, source in zip(loops, ids) if protected_vertices is None or source in protected_vertices)
    surviving = [[] for _ in faces]
    # A verified neighbor assignment can affect a loop's custom-normal space.
    # Resolve available neighborhoods first, then replay unresolved ones against
    # that context. This is a bounded conservative search, not a fallback match.
    for _ in range(4):
        previous = sum(bool(c) for c in surviving)
        for rank in range(maximum):
            proposed = [(surviving[i][0] if surviving[i] else choices[rank if rank < len(choices) else 0])[1]
                        for i, choices in enumerate(options)]
            projected = project(proposed)
            for i, (obj, loops) in enumerate(owners):
                if not surviving[i] and rank < len(options[i]) and agrees(i, projected, options[i][rank][1]):
                    # Do not finalize a rank without checking alternative identities.
                    surviving[i] = [options[i][rank]]
        if all(surviving) or sum(bool(c) for c in surviving) == previous:
            break
    # Also check every uniform-rank proposal. A local candidate may depend on
    # neighboring assignments; inconclusive cases remain conservatively blocked.
    filtered = [[] for _ in faces]
    for rank in range(maximum):
        proposed = [choices[rank if rank < len(choices) else 0][1] for choices in options]
        projected = project(proposed)
        for i, (obj, loops) in enumerate(owners):
            if rank < len(options[i]) and agrees(i, projected, options[i][rank][1]):
                filtered[i].append(options[i][rank])
    # Retain only proven semantic geometry or exact-normal evidence.
    for i in range(len(faces)):
        for choice in surviving[i]:
            if choice not in filtered[i]:
                filtered[i].append(choice)
    missing = next((i for i, choices in enumerate(filtered) if not choices), None)
    if missing is not None:
        obj, _ = owners[missing]
        hint = (' Blender Separate can re-encode corner normals. Use the original unsplit REF; '
                'reattaching after Separate is not supported in this release.' if len(objects) > 1 else
                ' Reimport the unchanged REF and verify its normals.')
        raise ValueError(f'{obj.name}: face {missing} has no exact source-normal projection.' + hint)
    mapped = (matching.resolve_candidates(document, filtered) if signatures is None else
              matching.resolve_candidates(document, filtered, signatures=signatures))
    final = project(mapped)
    for i, (obj, _) in enumerate(owners):
        require(agrees(i, final, mapped[i]),
                f'{obj.name}: final combined normal projection disagrees; attachment blocked')
    return mapped


def read_faces(objects):
    faces, owners = [], []
    require(objects, 'Select the complete REF mesh object set')
    seen = set()
    for obj in objects:
        require(obj.type == 'MESH' and obj.mode == 'OBJECT', 'Targets must be meshes in Object Mode')
        mesh = obj.data
        require(mesh.as_pointer() not in seen and mesh.users == 1, 'Shared/duplicate mesh data is unsupported')
        seen.add(mesh.as_pointer())
        require(all(abs(obj.matrix_world[i][j] - (1 if i == j else 0)) < 1e-7 for i in range(4) for j in range(4)),
                f'{obj.name}: requires reference-model axes, unit scale and unchanged origin')
        require(mesh.uv_layers.active is not None, f'{obj.name}: missing active UV layer')
        normals = mesh.corner_normals
        for poly in mesh.polygons:
            require(len(poly.loop_indices) == 3, f'{obj.name}: non-triangle polygon {poly.index}')
            require(poly.material_index < len(mesh.materials) and mesh.materials[poly.material_index] is not None,
                    f'{obj.name}: missing material')
            corners = []
            for index in poly.loop_indices:
                v = mesh.vertices[mesh.loops[index].vertex_index]
                corners.append({'position': list(v.co), 'normal': list(normals[index].vector),
                                'uv': list(mesh.uv_layers.active.data[index].uv),
                                'skinning': [(obj.vertex_groups[g.group].name, g.weight) for g in v.groups]})
            faces.append({'material': mesh.materials[poly.material_index].name, 'corners': corners})
            owners.append((obj, tuple(poly.loop_indices)))
    return faces, owners
