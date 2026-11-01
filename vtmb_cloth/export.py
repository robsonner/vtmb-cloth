"""Owned SMD adapter using captured exporter corner provenance.

This module never loads Blender or guesses identities from face order. The
Blender adapter must validate the owned REF before/after a controlled export.
"""
import copy
import hashlib
import json
import math
from collections import defaultdict
from . import matching, schema, smd
from .schema import require


def decimal6(value):
    return float(format(float(value), '.6f'))


def skin_key(links, normalize=False):
    values = defaultdict(float)
    for bone, weight in links:
        if weight > 0:
            values[bone] += weight
    total = sum(values.values())
    require(total > 0, 'Missing export skin weights')
    return tuple((bone, decimal6(weight/total if normalize else weight))
                 for bone,weight in sorted(values.items()) if decimal6(weight/total if normalize else weight)>0)


def corner_key(corner, normalize_skin=False):
    return (tuple(decimal6(v) for field in ('position','normal','uv') for v in corner[field]),
            skin_key(corner['skinning'],normalize_skin))


def oriented(values):
    values = tuple(values)
    return min(values[i:]+values[:i] for i in range(3))


def identity_map(document, actual, snapshots):
    """Match actual file corners to observed, ID-bearing export-stage meshes."""
    source_faces = defaultdict(list)
    for i,face in enumerate(document['reference']['triangles']):
        source_faces[(face['material'],oriented(face['vertices']))].append(i)
    actual_keys = [(f['material'],tuple(corner_key(c) for c in f['corners'])) for f in actual['faces']]
    combined = [set() for _ in actual_keys]
    compatible_snapshots = 0
    for snapshot in snapshots:
        if len(snapshot) != len(actual_keys):
            continue
        buckets = defaultdict(list)
        valid = True
        for face in snapshot:
            ids = tuple(c['source'] for c in face['corners'])
            original = source_faces.get((face['material'],oriented(ids)))
            if not original:
                valid = False
                break
            keys = tuple(c['key'] for c in face['corners'])
            buckets[(face['material'],oriented(keys))].append((keys,ids,original))
        if not valid:
            continue
        options = []
        for material, keys in actual_keys:
            choices = set()
            for candidate,ids,original in buckets.get((material,oriented(keys)),[]):
                for shift in range(3):
                    if candidate[shift:]+candidate[:shift] == keys:
                        rotated = ids[shift:]+ids[:shift]
                        choices.update((index,rotated) for index in original)
            options.append(choices)
        if all(options):
            compatible_snapshots += 1
            for all_choices,choices in zip(combined,options):
                all_choices.update(choices)
    require(compatible_snapshots, 'No captured export mesh exactly matches the actual SMD; output withheld')
    require(all(combined), 'Incomplete exported corner provenance')
    return matching.resolve_candidates(document,[sorted(v) for v in combined])


def _matrix_quaternion(q):
    x,y,z,w=q
    return [[1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)],
            [2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)],
            [2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)]]


def _matrix_euler(angles):
    x,y,z=angles
    cx,cy,cz=map(math.cos,(x,y,z))
    sx,sy,sz=map(math.sin,(x,y,z))
    return [[cy*cz,sx*sy*cz-cx*sz,cx*sy*cz+sx*sz],
            [cy*sz,sx*sy*sz+cx*cz,cx*sy*sz-sx*cz],[-sy,sx*cy,cx*cy]]


def _world(local):
    result={}
    def visit(name,stack):
        require(name not in stack,'Cyclic bone hierarchy')
        if name in result:
            return result[name]
        parent,r,p=local[name]
        scale=max(map(abs,p),default=0)
        if parent is not None:
            pr,pp,ps=visit(parent,stack|{name})
            r=[[sum(pr[i][k]*r[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
            p=[pp[i]+sum(pr[i][k]*p[k] for k in range(3)) for i in range(3)]
            scale=max(scale,ps,max(map(abs,p)))
        result[name]=(r,p,scale)
        return result[name]
    for name in local:
        visit(name,set())
    return result


def validate_pose(document, actual):
    source=document['reference']['bones']
    source_names={b['id']:b['name'] for b in source}
    bones=actual['bones']
    names={i:b['name'] for i,b in bones.items()}
    require(set(names.values())==set(source_names.values()),'Export changed the bone-name set')
    a={b['name']:(source_names[b['parent']] if b['parent']!=-1 else None,
                  _matrix_quaternion(b['rotation_xyzw']),b['position']) for b in source}
    b={bone['name']:(names[bone['parent']] if bone['parent']!=-1 else None,
                     _matrix_euler(actual['pose'][i][3:]),actual['pose'][i][:3]) for i,bone in bones.items()}
    require(all(a[n][0]==b[n][0] for n in a),'Export changed the bone hierarchy')
    aw,bw=_world(a),_world(b)
    epsilon=32*(2**-23)  # Documented native pose-check float32 error scale.
    for name in a:
        ar,ap,ascale=aw[name]
        br,bp,bscale=bw[name]
        require(all(abs(ar[i][j]-br[i][j]) <= epsilon*max(1,abs(ar[i][j]),abs(br[i][j]))
                    for i in range(3) for j in range(3)),f'Export changed reference rotation: {name}')
        require(all(abs(x-y)<=epsilon*max(1,ascale,bscale) for x,y in zip(ap,bp)),
                f'Export changed reference translation: {name}')


def adapt(document, raw_text, snapshots, reference_name, original_reference):
    """Replay canonical positions/UVs/normals/weights after provenance proof.

    Actual file order and bone numbering remain authoritative. Positions/UVs
    must equal canonical values or their exact six-decimal SMD conversion;
    replay canonical precision only after observed corner provenance proof.
    The caller proves the live rig matches a control import of original_reference;
    preserve that source pose rather than exporter armature/Euler roundoff.
    Simulation/reference tables remain unchanged except the relative REF filename.
    Caller supplies snapshots observed during its own controlled exporter run.
    """
    actual=smd.loads(raw_text)
    require(isinstance(reference_name,str) and reference_name and not any(c in reference_name for c in ('/','\\','\n','\r','\x00')),
            'REF output must be a relative filename without directories')
    validate_pose(document,original_reference)
    source_names={b['name']:i for i,b in original_reference['bones'].items()}
    actual_names={b['name']:i for i,b in actual['bones'].items()}
    require(source_names.keys()==actual_names.keys(),'Exporter changed the bone-name set')
    for name,i in actual_names.items():
        source_bone=original_reference['bones'][source_names[name]]
        actual_parent=actual['bones'][i]['parent']
        source_parent=source_bone['parent']
        require((actual['bones'][actual_parent]['name'] if actual_parent!=-1 else None)==
                (original_reference['bones'][source_parent]['name'] if source_parent!=-1 else None),
                'Exporter changed the bone hierarchy')
    mapped=identity_map(document,actual,snapshots)
    names={b['id']:b['name'] for b in document['reference']['bones']}
    actual_ids={b['name']:i for i,b in actual['bones'].items()}
    lines=['version 1','nodes']
    for i,bone in actual['bones'].items():
        require('"' not in bone['name'] and '\n' not in bone['name'],'Unsafe SMD bone name')
        lines.append(f'{i} "{bone["name"]}" {bone["parent"]}')
    lines+=['end','skeleton','time 0']
    number=lambda v:format(float(v),'.17g')
    pose_changes=0
    for i,pose in actual['pose'].items():
        original_pose=original_reference['pose'][source_names[actual['bones'][i]['name']]]
        pose_changes+=pose!=original_pose
        lines.append(str(i)+' '+' '.join(map(number,original_pose)))
    lines+=['end','triangles']
    normal_changes=skin_changes=position_changes=uv_changes=0
    for face,ids in zip(actual['faces'],mapped):
        require('\n' not in face['material'] and face['material']!='end','Unsafe material')
        lines.append(face['material'])
        for corner,source in zip(face['corners'],ids):
            original=document['reference']['vertices'][source]['exported']
            # This is an exact documented text conversion, not a spatial
            # tolerance. Caller already proved the live owned mesh positions.
            require(corner['position'] in (original['position'], [decimal6(v) for v in original['position']])
                    and corner['uv'] in (original['uv'], [decimal6(v) for v in original['uv']]),
                    'Exporter changed REF positions or UVs; output withheld')
            position_changes+=corner['position']!=original['position']
            uv_changes+=corner['uv']!=original['uv']
            normal_changes+=corner['normal']!=original['normal']
            canonical_links=[(names[bone],weight) for bone,weight in original['skinning']]
            skin_changes+=skin_key(corner['skinning'])!=skin_key(canonical_links)
            parent=actual_ids[names[original['parent_bone']]]
            values=original['position']+original['normal']+original['uv']
            links=''.join(f' {actual_ids[bone]} {number(weight)}' for bone,weight in canonical_links)
            lines.append(f'{parent} '+' '.join(map(number,values))+f' {len(canonical_links)}'+links)
    lines+=['end','']
    output='\n'.join(lines)
    # Verify the ACTUAL final text, not only the in-memory proposed records.
    final=smd.loads(output)
    validate_pose(document,final)
    options=matching.candidates(document,final['faces'],matching.compatible)
    matching.resolve_candidates(document,options)
    sidecar=copy.deepcopy(document)
    sidecar['reference_smd']=reference_name
    schema.validate(sidecar)
    report={'faces':len(mapped),'restored_position_corners':position_changes,'restored_uv_corners':uv_changes,'restored_normal_corners':normal_changes,'restored_weight_corners':skin_changes,'preserved_bindpose_bones':pose_changes,
            'raw_smd_sha256':hashlib.sha256(raw_text.encode()).hexdigest(),
            'final_smd_sha256':hashlib.sha256(output.encode()).hexdigest(),
            'simulation_unchanged':sidecar['simulation']==document['simulation']}
    return output,schema.dumps(sidecar),report
