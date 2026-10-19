import sys,unittest
sys.path.insert(0,str(__import__('pathlib').Path(__file__).resolve().parent))
from request_modes import request_modes,InvalidMode
class Tests(unittest.TestCase):
 def test_all_efforts_independent_of_speed(self):
  for e in ['low','medium','high','xhigh','max']:
   for speed in ['standard','fast']:
    r=request_modes({'output_config':{'effort':e},'speed':speed})
    self.assertEqual(r['reasoning']['effort'],e)
    self.assertEqual(r.get('service_tier'), 'priority' if speed=='fast' else 'default')
 def test_missing_preserves_low(self):self.assertEqual(request_modes({}),{'reasoning':{'effort':'low'},'service_tier':'default'})
 def test_invalid_fails(self):
  for b in [{'output_config':{'effort':'ultra'}},{'speed':'faster'}]:
   with self.assertRaises(InvalidMode):request_modes(b)
if __name__=='__main__':unittest.main()
