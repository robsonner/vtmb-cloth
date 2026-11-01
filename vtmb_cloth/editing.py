"""Experimental raw spring coefficient edits; no geometry/topology synthesis."""
import copy
import struct
from . import schema
from .schema import require

FIELDS=('weight_a','weight_b')


def selected_springs(document, nodes, rule='ANY'):
    require(rule in ('ANY','BOTH'), 'Unknown spring selection rule')
    nodes=set(nodes)
    return [i for i,spring in enumerate(document['simulation']['springs'])
            if (any(n in nodes for n in spring['nodes']) if rule=='ANY'
                else all(n in nodes for n in spring['nodes']))]


def scale_springs(document, spring_ids, multiplier, endpoint='BOTH'):
    """Scale raw factors in one preserving, atomic batch; not stiffness units."""
    schema.validate(document)
    schema.scalar(multiplier)
    require(multiplier>0, 'Multiplier must be positive')
    require(endpoint in ('A','B','BOTH'), 'Unknown endpoint selection')
    ids=sorted(set(spring_ids))
    require(ids, 'No springs match the selected mesh vertices')
    fields=FIELDS if endpoint=='BOTH' else (FIELDS[0 if endpoint=='A' else 1],)
    result=copy.deepcopy(document)
    entries=[]
    for index in ids:
        schema.integer(index,len(document['simulation']['springs'])-1)
        spring=document['simulation']['springs'][index]
        changes={}
        for field in fields:
            old=spring[field]
            if old==0:
                continue
            value=float32(old*multiplier)
            require(value!=0, 'Multiplier would erase a nonzero endpoint factor')
            if struct.pack('<f',old)!=struct.pack('<f',value):
                result['simulation']['springs'][index][field]=value
                changes[field]={'before':old,'after':value}
        if changes:
            entries.append({'spring':index,'changes':changes})
    schema.validate(result)
    masked=copy.deepcopy(result)
    for entry in entries:
        for field,change in entry['changes'].items():
            masked['simulation']['springs'][entry['spring']][field]=change['before']
    require(masked==document,'Unexpected change outside requested spring factors')
    return result,entries


def float32(value):
    schema.scalar(value)
    return struct.unpack('<f',struct.pack('<f',value))[0]


def edit_spring(document, spring_id, weight_a, weight_b):
    """Change only two endpoint factors on one existing spring.

    Zero/nonzero pattern, signs, endpoints, family, rest length, all surface
    tables and SIMD ordering remain intact. This validates format constraints,
    not physical stability or an inferred Troika stiffness interpretation.
    """
    schema.validate(document)
    springs=document['simulation']['springs']
    schema.integer(spring_id,len(springs)-1)
    previous=springs[spring_id]
    values={'weight_a':float32(weight_a),'weight_b':float32(weight_b)}
    require(values['weight_a']>=0 and values['weight_b']<=0,'Endpoint A must be nonnegative and B nonpositive')
    for field,value in values.items():
        require((previous[field]==0)==(value==0),'Zero endpoint factors are locked; preserve the zero/nonzero pattern')
    result=copy.deepcopy(document)
    changes={}
    for field,value in values.items():
        old=previous[field]
        if struct.pack('<f',old)!=struct.pack('<f',value) and not(old==value==0):
            result['simulation']['springs'][spring_id][field]=value
            changes[field]={'before':old,'after':value}
    schema.validate(result)
    # Prove that no dependent/unknown field or table was silently regenerated.
    masked=copy.deepcopy(result)
    for field in FIELDS:
        masked['simulation']['springs'][spring_id][field]=previous[field]
    require(masked==document,'Unexpected change outside the requested spring factors')
    return result,{'spring':spring_id,'changes':changes}
