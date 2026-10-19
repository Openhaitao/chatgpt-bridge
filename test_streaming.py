import sys,json,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from sse_bridge import read_response,ResponseFailed

def complete(output):return {'type':'response.completed','response':{'status':'completed','output':output}}
class Response:
 def __init__(self,events,before=None):self.events=events;self.before=before
 def iter_lines(self,**kwargs):
  assert kwargs['chunk_size']==1
  for e in self.events:
   if e['type']=='response.completed' and self.before:self.before()
   yield ('data: '+json.dumps(e)).encode()
class Tests(unittest.TestCase):
 def test_text_is_delivered_before_completion(self):
  emitted=[]
  def before():self.assertTrue(any(n=='content_block_delta' for n,_ in emitted))
  out=[{'type':'message','content':[{'type':'output_text','text':'hello world'}]}]
  ev=[{'type':'response.output_text.delta','output_index':0,'content_index':0,'delta':'hello '},{'type':'response.output_text.delta','output_index':0,'content_index':0,'delta':'world'},complete(out)]
  read_response(Response(ev,before),lambda *x:emitted.append(x))
  self.assertEqual(''.join(d['delta']['text'] for n,d in emitted if n=='content_block_delta'),'hello world')
  self.assertEqual(sum(n=='content_block_start' for n,_ in emitted),1)
 def test_tool_arguments_are_streamed_once_and_keep_call_identity(self):
  out=[{'type':'reasoning'}, {'type':'function_call','name':'Bash','call_id':'call_abc','arguments':'{"command":"pwd"}'}]
  ev=[{'type':'response.output_item.added','output_index':1,'item':out[1]}, {'type':'response.function_call_arguments.delta','output_index':1,'delta':'{"command":'}, {'type':'response.function_call_arguments.delta','output_index':1,'delta':'"pwd"}'},{'type':'response.output_item.done','output_index':1,'item':out[1]},complete(out)]
  emitted=[];read_response(Response(ev),lambda *x:emitted.append(x))
  self.assertEqual(next(d['content_block']['id'] for n,d in emitted if n=='content_block_start'),'call_abc')
  self.assertEqual(json.loads(''.join(d['delta']['partial_json'] for n,d in emitted if n=='content_block_delta')),{'command':'pwd'})
  self.assertEqual(sum(n=='content_block_stop' for n,_ in emitted),1)
 def test_final_only_transport_still_works(self):
  e=[];read_response(Response([complete([{'type':'message','content':[{'type':'output_text','text':'OK'}]}])]),lambda *x:e.append(x))
  self.assertEqual([d['delta']['text'] for n,d in e if n=='content_block_delta'],['OK'])
 def test_multiple_text_parts_not_replayed(self):
  events=[{'type':'response.output_text.delta','output_index':0,'content_index':i,'delta':str(i)} for i in [0,1]]
  events.append(complete([{'type':'message','content':[{'type':'output_text','text':'0'},{'type':'output_text','text':'1'}]}]))
  e=[];read_response(Response(events),lambda *x:e.append(x));self.assertEqual(sum(n=='content_block_start' for n,_ in e),2)
 def test_upstream_failure_is_not_reported_as_success(self):
  with self.assertRaises(ResponseFailed):read_response(Response([{'type':'response.failed'}]),lambda *_:None)
 def test_truncated_stream_is_not_success(self):
  with self.assertRaises(ResponseFailed):read_response(Response([{'type':'response.output_text.delta','delta':'partial'}]),lambda *_:None)
 def test_tool_delta_without_identity_is_rejected(self):
  with self.assertRaises(ResponseFailed):read_response(Response([{'type':'response.function_call_arguments.delta','delta':'{}'}]),lambda *_:None)
if __name__=='__main__':unittest.main()
