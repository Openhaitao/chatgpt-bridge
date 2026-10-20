"""Entirely offline HTTP and launcher regression tests with temporary credentials."""
import contextlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gateway
from bridge_support import controls, tool_output, InvalidRequest, safe_detail, upstream_error
from sse_bridge import read_response
loader = importlib.machinery.SourceFileLoader('bridge_launcher', str(Path(__file__).resolve().parent.parent / 'claude'))
spec = importlib.util.spec_from_loader(loader.name, loader)
launcher = importlib.util.module_from_spec(spec)
loader.exec_module(launcher)

@contextlib.contextmanager
def serving(handler):
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

class Fake(BaseHTTPRequestHandler):
    status = 200
    received = None
    error = 'invalid max_output_tokens'
    events = None
    calls = 0
    def log_message(self, *args): pass
    def do_POST(self):
        Fake.calls += 1
        Fake.received = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.send_response(Fake.status)
        self.end_headers()
        if Fake.status != 200:
            self.wfile.write(json.dumps({'error': {'message': Fake.error}}).encode())
            return
        if Fake.events is not None:
            for event in Fake.events:
                self.wfile.write(('data: '+json.dumps(event)+'\n\n').encode())
            return
        self.wfile.write(('data: ' + json.dumps({'type':'response.completed','response':{'status':'completed','model':'gpt-6-astra','output':[{'type':'message','content':[{'type':'output_text','text':'OK'}]}],'usage':{'input_tokens':3,'output_tokens':1}}}) + '\n\n').encode())

class Tests(unittest.TestCase):
    def test_controls(self):
        self.assertEqual(controls({'max_tokens':32000,'stream':True,'tool_choice':{'type':'auto'}}), {'max_output_tokens':32000,'tool_choice':'auto'})
        for value in [0, -1, True, '3']:
            with self.assertRaises(InvalidRequest): controls({'max_tokens':value})
        with self.assertRaises(InvalidRequest): controls({'stop_sequences':['END']})
        self.assertEqual(controls({'tools':[{'name':'x'}],'tool_choice':{'type':'tool','name':'x','disable_parallel_tool_use':True}}), {'tool_choice':{'type':'function','name':'x'},'parallel_tool_calls':False})
        self.assertEqual(controls({'tools':[{'name':'x'}],'tool_choice':{'type':'any'}})['tool_choice'],'required')
        self.assertEqual(controls({'tool_choice':{'type':'none'}})['tool_choice'],'none')
        for choice in [{'type':'any'}, {'type':'tool','name':'missing'}, {'type':'unknown'}, {'type':'auto','disable_parallel_tool_use':'yes'}]:
            with self.assertRaises(InvalidRequest): controls({'tool_choice':choice})

    def test_images_are_not_json_text(self):
        result = tool_output([{'type':'text','text':'screenshot'}, {'type':'image','source':{'type':'base64','media_type':'image/png','data':'AAAA'}}, {'type':'image','source':{'type':'url','url':'https://example.invalid/image.png'}}])
        self.assertEqual(result[1], {'type':'input_image','image_url':'data:image/png;base64,AAAA'})
        self.assertEqual(result[2]['type'],'input_image')
        self.assertEqual(tool_output('plain'), 'plain')
        with self.assertRaises(InvalidRequest): tool_output([{'type':'document'}])

    def test_tool_final_fallback_after_item_done(self):
        from test_streaming import Response, complete
        for done in [{}, {'arguments':'{"x":1}'}]:
            item={'type':'function_call','name':'x','call_id':'c','arguments':'{"x":1}'}
            events=[{'type':'response.output_item.added','item':item}, {'type':'response.output_item.done','item':done}, complete([item])]
            emitted=[]
            read_response(Response(events), lambda *args:emitted.append(args))
            self.assertEqual(''.join(d['delta']['partial_json'] for n,d in emitted if n=='content_block_delta'),'{"x":1}')
            self.assertEqual(sum(n=='content_block_stop' for n,d in emitted),1)

    def test_token_limit_text_and_tool(self):
        from test_streaming import Response
        result={'status':'incomplete','incomplete_details':{'reason':'max_output_tokens'},'output':[{'type':'message','content':[{'type':'output_text','text':'partial'}]}]}
        for emit in [None,lambda *_:None]:
            actual,_=read_response(Response([{'type':'response.incomplete','response':result}]),emit)
            self.assertEqual(actual['status'],'incomplete')
        result['output']=[{'type':'function_call','call_id':'c','name':'x','arguments':'{"x":'}]
        from sse_bridge import ResponseFailed
        with self.assertRaises(ResponseFailed):read_response(Response([{'type':'response.incomplete','response':result}]))

    def test_sanitizer(self):
        value=safe_detail('secret-local Bearer abc sk-example access_token=xyz eyJabc.def', ['secret-local'])
        for secret in ['secret-local','abc','sk-example','xyz','eyJabc.def']:
            self.assertNotIn(secret,value)

    def test_upstream_error_preserves_safe_json_detail(self):
        class Response:
            status_code = 400
            def __init__(self, payload): self.payload = payload
            def json(self): return self.payload
        cases = [
            ({'error': {'message': 'invalid model'}}, 'invalid model'),
            ({'error': {'detail': 'invalid request'}}, 'invalid request'),
            ({'detail': 'model is unavailable for this account'}, 'model is unavailable for this account'),
        ]
        for payload, expected in cases:
            status, error = upstream_error(Response(payload), ['private-token'])
            self.assertEqual(status, 400)
            self.assertEqual(error['error']['message'], expected)
            self.assertEqual(error['error']['upstream_status'], 400)

    def test_account_model_catalog_limits_listing_and_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'config.json'; auth=Path(directory)/'auth.json'
            config.write_text(json.dumps({'local_token':'test-local','available_models':['gpt-6-luna'],'model_aliases':{'allowed':'gpt-6-luna','blocked':'gpt-6-astra'}}))
            auth.write_text(json.dumps({'tokens':{'access_token':'test-access','account_id':'test-account'}}))
            with patch.object(gateway,'CONFIG',config), patch.object(gateway,'AUTH',auth), serving(gateway.H) as url:
                headers={'Authorization':'Bearer test-local'}
                listed=requests.get(url+'/v1/models').json()['data']
                self.assertEqual([model['id'] for model in listed], ['gpt-6-luna'])
                body={'model':'blocked','messages':[{'role':'user','content':'hello'}],'tool_choice':{'type':'auto'}}
                rejected=requests.post(url+'/v1/messages',headers=headers,json=body)
                self.assertEqual(rejected.status_code,400)
                self.assertIn('current Codex account catalog',rejected.json()['error']['message'])

    def test_retries_chunked_upstream_failure_before_sse_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'config.json'; auth=Path(directory)/'auth.json'
            config.write_text(json.dumps({'local_token':'test-local','model_aliases':{'alias':'gpt-6-astra'}}))
            auth.write_text(json.dumps({'tokens':{'access_token':'test-access','account_id':'test-account'}}))
            complete={'status':'completed','model':'gpt-6-astra','output':[{'type':'message','content':[{'type':'output_text','text':'OK'}]}],'usage':{'input_tokens':3,'output_tokens':1}}
            Fake.status=200; Fake.calls=0
            with serving(Fake) as upstream, patch.object(gateway,'CONFIG',config), patch.object(gateway,'AUTH',auth), patch.object(gateway,'UPSTREAM',upstream), patch.object(gateway,'read_response',side_effect=[requests.exceptions.ChunkedEncodingError('interrupted'),(complete,None)]), serving(gateway.H) as url:
                body={'model':'alias','messages':[{'role':'user','content':'hello'}],'stream':False,'tool_choice':{'type':'auto'}}
                response=requests.post(url+'/v1/messages',headers={'Authorization':'Bearer test-local'},json=body)
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.json()['content'][0]['text'],'OK')
                self.assertEqual(Fake.calls,2)

    def test_http_fake_upstream(self):
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'config.json'; auth=Path(directory)/'auth.json'
            config.write_text(json.dumps({'local_token':'test-local','model_aliases':{'alias':'gpt-6-astra'}}))
            auth.write_text(json.dumps({'tokens':{'access_token':'test-access','account_id':'test-account'}}))
            Fake.status=200
            with serving(Fake) as upstream, patch.object(gateway,'CONFIG',config), patch.object(gateway,'AUTH',auth), patch.object(gateway,'UPSTREAM',upstream), serving(gateway.H) as url:
                headers={'Authorization':'Bearer test-local'}
                body={'model':'alias','max_tokens':32000,'messages':[{'role':'user','content':[{'type':'tool_result','tool_use_id':'c','content':[{'type':'image','source':{'type':'base64','media_type':'image/png','data':'AAAA'}}]}]}],'tool_choice':{'type':'auto'}}
                self.assertEqual(requests.get(url+'/health',headers=headers).status_code,200)
                self.assertEqual(requests.get(url+'/health').status_code,401)
                self.assertEqual(requests.post(url+'/v1/messages',json=body).status_code,401)
                for stream in [False,True]:
                    body['stream']=stream
                    response=requests.post(url+'/v1/messages',headers=headers,json=body)
                    self.assertEqual(response.status_code,200)
                    if stream:
                        self.assertIn('message_stop',response.text)
                    else:self.assertEqual(response.json()['content'][0]['text'],'OK')
                    self.assertEqual(Fake.received['max_output_tokens'],32000)
                    self.assertEqual(Fake.received['input'][0]['output'][0]['type'],'input_image')
                for status in [400,401,403,429,500,503]:
                    Fake.status=status
                    Fake.error='bad test-access test-account test-local Bearer unknown access_token=private'
                    response=requests.post(url+'/v1/messages',headers=headers,json=body)
                    self.assertEqual(response.status_code,status)
                    for secret in ['test-access','test-account','test-local','unknown','private']:
                        self.assertNotIn(secret,response.text)
                Fake.status=200
                Fake.events=[{'type':'response.output_text.delta','delta':'partial'}, {'type':'response.failed','response':{'error':{'message':'failure test-access Bearer hidden'}}}]
                try:
                    failure=requests.post(url+'/v1/messages',headers=headers,json=body)
                    self.assertEqual(failure.status_code,200)
                    self.assertIn('event: error',failure.text)
                    self.assertNotIn('message_stop',failure.text)
                    self.assertNotIn('test-access',failure.text)
                    self.assertNotIn('hidden',failure.text)
                finally:Fake.events=None
                bad=dict(body,stop_sequences=['STOP'])
                self.assertEqual(requests.post(url+'/v1/messages',headers=headers,json=bad).status_code,400)

    def test_launcher_wrong_service_never_spawns(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(launcher,'health',return_value='wrong'), patch.object(launcher.subprocess,'Popen') as spawn:
            with self.assertRaises(RuntimeError): launcher.ensure_gateway('http://127.0.0.1:1','temp',{'BRIDGE_STATE_DIR':directory})
            spawn.assert_not_called()

    def test_launcher_ready_never_spawns(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(launcher,'health',return_value='ready'), patch.object(launcher.subprocess,'Popen') as spawn:
            launcher.ensure_gateway('http://127.0.0.1:1','temp',{'BRIDGE_STATE_DIR':directory})
            spawn.assert_not_called()

    def test_launcher_spawn_and_wait(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(launcher,'health',side_effect=['absent','absent','ready']), patch.object(launcher.time,'sleep'), patch.object(launcher.subprocess,'Popen') as spawn:
            spawn.return_value.poll.return_value=None
            launcher.ensure_gateway('http://127.0.0.1:1','temp',{'BRIDGE_STATE_DIR':directory,'BRIDGE_PYTHON':'/temp/python','BRIDGE_GATEWAY':'/temp/gateway.py'})
            self.assertEqual(spawn.call_args.args[0],['/temp/python','/temp/gateway.py'])
            self.assertEqual(spawn.call_args.kwargs['env']['PYTHONDONTWRITEBYTECODE'],'1')

    def test_launcher_timeout_terminates_child(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(launcher,'health',return_value='absent'), patch.object(launcher.time,'sleep'), patch.object(launcher.subprocess,'Popen') as spawn:
            spawn.return_value.poll.return_value=None
            with self.assertRaises(RuntimeError): launcher.ensure_gateway('http://127.0.0.1:1','temp',{'BRIDGE_STATE_DIR':directory})
            spawn.return_value.terminate.assert_called_once()
            spawn.return_value.wait.assert_called_once()

    def test_real_local_health_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'config.json'
            config.write_text(json.dumps({'local_token':'temporary'}))
            with patch.object(gateway,'CONFIG',config), serving(gateway.H) as url:
                self.assertEqual(launcher.health(url,'temporary'),'ready')
                self.assertEqual(launcher.health(url,'wrong'),'wrong')
            with serving(Fake) as url:
                self.assertEqual(launcher.health(url,'temporary'),'wrong')

    def test_launcher_child_exit(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(launcher,'health',return_value='absent'), patch.object(launcher.subprocess,'Popen') as spawn:
            spawn.return_value.poll.return_value=1
            with self.assertRaises(RuntimeError): launcher.ensure_gateway('http://127.0.0.1:1','temp',{'BRIDGE_STATE_DIR':directory})

if __name__ == '__main__': unittest.main()
