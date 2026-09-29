"""Approximate spatial comparison for current paths; never asserts road identity."""
import hashlib

def grid_of_path(path):
    points=set()
    for step in path.get('steps',[]):
        for pair in step.get('polyline','').split(';'):
            try:
                lon,lat=map(float,pair.split(','))
                points.add((round(lon,2),round(lat,2)))
            except ValueError:
                continue
    return points

def fingerprint(path):
    line=';'.join(s.get('polyline','') for s in path.get('steps',[]))
    return hashlib.sha256(line.encode()).hexdigest()[:20]

def overlap(a,b):
    a,b=set(map(tuple,a)),set(map(tuple,b))
    return len(a&b)/len(a|b) if a and b else 0

def select_candidates(paths):
    selected=[]
    valid=[]
    for p in paths:
        try:
            if grid_of_path(p) and 0 < float(p.get('duration') or 0) < float('inf') and 0 < float(p.get('distance') or 0) < float('inf'):
                valid.append(p)
        except (ValueError, TypeError):
            continue
    for p in sorted(valid,key=lambda x:float(x['duration'])):
        if any(fingerprint(p)==fingerprint(old) or (overlap(grid_of_path(p),grid_of_path(old))>=.98 and abs(float(p['distance'])-float(old['distance']))/max(float(old['distance']),1)<.01) for old in selected):
            continue
        selected.append(p)
    return selected[:2]

def annotate_comparisons(items):
    fixed=[x for x in items if x.get('ok') and not x.get('dynamic')]
    for x in items:
        if not x.get('ok') or not x.get('dynamic'):continue
        scores=[(overlap(x.get('route_grid',[]),f.get('route_grid',[])),f) for f in fixed]
        if not scores:continue
        score,f=max(scores,key=lambda row:row[0])
        x['comparison']={'closest_id':f['id'],'closest_short':f['short'],'grid_overlap':round(score,3),
          'similar':score>=.85,'distance_delta_km':round(x['distance_km']-f['distance_km'],1),
          'duration_delta_min':round(x['duration_min']-f['duration_min'],1)}
