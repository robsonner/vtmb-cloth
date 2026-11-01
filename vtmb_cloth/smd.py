"""Strict dependency-free reader for actual reference SMD v1 output."""
import math
import shlex
from pathlib import Path
from .schema import require


def loads(text):
    lines = iter(enumerate(text.splitlines(), 1))

    def take():
        for number, raw in lines:
            value = raw.strip()
            if value and not value.startswith('//'):
                return number, value
        raise ValueError('Unexpected end of SMD')

    def real(token):
        value = float(token)
        require(math.isfinite(value), 'Nonfinite SMD number')
        return value

    try:
        require(take()[1] == 'version 1', 'Only SMD version 1 is supported')
        require(take()[1] == 'nodes', 'Missing SMD nodes')
        bones = {}
        while True:
            _, line = take()
            if line == 'end':
                break
            fields = shlex.split(line)
            require(len(fields) == 3, 'Malformed SMD bone')
            index, name, parent = int(fields[0]), fields[1], int(fields[2])
            require(index >= 0 and index not in bones, 'Duplicate/negative SMD bone ID')
            require(name not in {b['name'] for b in bones.values()}, 'Duplicate SMD bone name')
            bones[index] = {'name':name, 'parent':parent}
        require(bones and all(b['parent'] == -1 or b['parent'] in bones for b in bones.values()), 'Invalid SMD hierarchy')
        for index in bones:
            seen=set()
            while index!=-1:
                require(index not in seen,'Cyclic SMD hierarchy')
                seen.add(index)
                index=bones[index]['parent']
        require(take()[1] == 'skeleton', 'Missing SMD skeleton')
        require(take()[1] == 'time 0', 'REF must contain only time 0')
        pose = {}
        while True:
            _, line = take()
            if line == 'end':
                break
            fields = line.split()
            require(len(fields) == 7 and fields[0] != 'time', 'REF requires a single complete pose')
            index = int(fields[0])
            require(index in bones and index not in pose, 'Invalid/duplicate SMD pose bone')
            pose[index] = [real(v) for v in fields[1:]]
        require(pose.keys() == bones.keys(), 'Incomplete SMD pose')
        require(take()[1] == 'triangles', 'Missing SMD triangles')
        faces = []
        while True:
            _, material = take()
            if material == 'end':
                break
            corners = []
            for _ in range(3):
                _, line = take()
                f = line.split()
                require(len(f) >= 9, 'Incomplete SMD corner')
                parent = int(f[0])
                require(parent in bones, 'Unknown SMD parent bone')
                values = [real(v) for v in f[1:9]]
                count = int(f[9]) if len(f) > 9 else 0
                require(count >= 0 and len(f) == (10+2*count if len(f)>9 else 9), 'Malformed SMD weights')
                links = []
                for i in range(count):
                    bone, weight = int(f[10+2*i]), real(f[11+2*i])
                    require(bone in bones and weight >= 0, 'Invalid SMD skinning')
                    links.append((bones[bone]['name'], weight))
                if not count:
                    links = [(bones[parent]['name'],1.0)]
                require(sum(w for _,w in links)>0, 'Missing SMD weights')
                corners.append({'position':values[:3], 'normal':values[3:6], 'uv':values[6:],
                                'skinning':links, 'parent_bone':parent})
            faces.append({'material':material, 'corners':corners})
        require(faces, 'Empty SMD geometry')
        require(not any(raw.strip() and not raw.strip().startswith('//') for _,raw in lines), 'Trailing SMD data')
        return {'bones':bones,'pose':pose,'faces':faces}
    except (IndexError, TypeError, OverflowError) as exc:
        raise ValueError(f'Malformed SMD: {exc}') from exc


def read(path):
    return loads(Path(path).read_text(encoding='utf-8-sig'))
