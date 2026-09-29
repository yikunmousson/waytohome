import sys,types,unittest,copy
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
sys.modules.setdefault('requests',types.ModuleType('requests'))
from route_compare import select_candidates,annotate_comparisons
from collect import collect_direction
from test_analysis import config

class RouteTests(unittest.TestCase):
 def path(self,duration,lat):
  p=copy.deepcopy(__import__('json').loads((Path(__file__).parent/'fixture_amap.json').read_text())['route']['paths'][0])
  p['duration']=str(duration)
  for step in p['steps']: step['polyline']='113.00,%s;113.01,%s'%(lat,lat)
  return p
 def test_select_sorted_unique(self):
  slow=self.path(30000,22);fast=self.path(20000,23)
  self.assertEqual([p['duration'] for p in select_candidates([slow,fast,fast])],['20000','30000'])
 def test_missing_second_and_one_request(self):
  cfg=config();cfg['trip'].update(dynamic_candidates=True,origin='113,22',destination='112,26');cfg['analysis']['baseline_bucket_minutes']=30
  payload={'route':{'paths':[self.path(20000,23)]}}
  class Client:
   source='fixture';call_count=0
   def driving_v3(self,*args,**kwargs):
    self.call_count+=1
    if kwargs['strategy']==10: assert kwargs['waypoints'] is None
    return payload
  c=Client();result=collect_direction(c,cfg,'outbound',cfg['trip'])
  self.assertEqual(c.call_count,2)
  self.assertEqual(len(result['corridors']),3)
  self.assertTrue(result['corridors'][1]['ok'])
  self.assertFalse(result['corridors'][2]['ok'])
  self.assertTrue(result['corridors'][1]['comparison']['similar'])
 def test_overlap_and_delta(self):
  rows=[{'id':'fixed','short':'固定','ok':True,'route_grid':[[1,2]],'distance_km':600,'duration_min':400},
        {'id':'dynamic','dynamic':True,'ok':True,'route_grid':[[1,2]],'distance_km':605,'duration_min':390}]
  annotate_comparisons(rows)
  self.assertEqual(rows[1]['comparison']['duration_delta_min'],-10)
  self.assertEqual(rows[1]['comparison']['distance_delta_km'],5)
