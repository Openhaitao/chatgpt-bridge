import json,os,uuid,requests,time
from sse_bridge import read_response, ResponseFailed, is_token_limit
from request_modes import request_modes, InvalidMode
from bridge_support import controls, image, tool_output, InvalidRequest, upstream_error, safe_detail
from codex_auth import AuthStore, AuthError, authorized_post
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
BASE=Path(__file__).resolve().parent
MODEL='gpt-6-astra'
CONFIG=Path(os.environ.get('BRIDGE_CONFIG', Path.home()/'.config/claude-chatgpt/gateway.json'))
AUTH=Path(os.environ.get('BRIDGE_CODEX_AUTH', Path.home()/'.codex/auth.json'))
UPSTREAM=os.environ.get('BRIDGE_UPSTREAM_URL','https://chatgpt.com/backend-api/codex/responses')
MODELS=['gpt-6.1-sol','gpt-6-astra','gpt-6-sol','gpt-6-luna','gpt-5.6-sol','gpt-5.6-terra','gpt-5.6-luna']

def available_models(config):
 models=config.get('available_models',MODELS)
 if not isinstance(models,list) or not models or any(not isinstance(model,str) or model not in MODELS for model in models):
  raise ValueError('Invalid available_models configuration')
 return models

class H(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_GET(self):
  if self.path == '/health':
   config=json.loads(CONFIG.read_text())
   if self.headers.get('Authorization') != 'Bearer '+config['local_token']:return self.sendj({'error':{'message':'Local auth required'}},401)
   return self.sendj({'service':'claude-chatgpt-bridge','protocol':1})
  if self.path not in ('/v1/models','/models'):return self.sendj({'error':{'message':'Not found'}},404)
  models=available_models(json.loads(CONFIG.read_text()))
  self.sendj({'data':[{'id':m,'type':'model','display_name':m} for m in models]})
 def sendj(self,obj,code=200):
  d=json.dumps(obj).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(d)));self.end_headers();self.wfile.write(d)
 def do_POST(self):
  try:
   streaming_started=False
   started=time.monotonic()
   config=json.loads(CONFIG.read_text())
   models=available_models(config)
   local_token=config['local_token']
   secrets=[local_token]
   streaming_started=False
   if self.headers.get('Authorization')!='Bearer '+local_token:return self.sendj({'error':{'type':'authentication_error','message':'Local auth required'}},401)
   length=int(self.headers.get('Content-Length',0))
   if not 0 < length <= 33554432:return self.sendj({'error':{'message':'Invalid request size'}},413)
   b=json.loads(self.rfile.read(length))
   if self.path.startswith('/v1/messages/count_tokens'):return self.sendj({'input_tokens':len(json.dumps(b))//3})
   if self.path not in ('/v1/messages','/v1/messages?beta=true'):return self.sendj({'type':'error','error':{'type':'not_found_error','message':'Not found'}},404)
   if self.headers.get('Authorization')!='Bearer '+local_token:return self.sendj({'error':{'type':'authentication_error','message':'Local auth required'}},401)
   if not isinstance(b,dict):raise InvalidRequest('Request must be an object')
   extra=controls(b)
   inp=[]
   for m in b.get('messages',[]):
    content=m.get('content',[])
    if isinstance(content,str):content=[{'type':'text','text':content}]
    for x in content:
     typ=x.get('type')
     if typ=='text':inp.append({'role':m['role'],'content':x['text']})
     elif typ=='image':
      inp.append({'role':m['role'],'content':[image(x)]})
     elif typ=='tool_use':inp.append({'type':'function_call','call_id':x['id'],'name':x['name'],'arguments':json.dumps(x['input'])})
     elif typ=='tool_result':
      v=x.get('content','');inp.append({'type':'function_call_output','call_id':x['tool_use_id'],'output':tool_output(v)})
   system=b.get('system','');instructions=system if isinstance(system,str) else '\n'.join(x.get('text','') for x in system)
   tools=[{'type':'function','name':t['name'],'description':t.get('description',''),'parameters':t.get('input_schema',{'type':'object','properties':{}}),'strict':False} for t in b.get('tools',[]) if 'input_schema' in t]
   requested=b.get('model',MODEL)
   aliases=config.get('model_aliases',{})
   normalized=requested.removesuffix('[1m]')
   selected=normalized if normalized in MODELS else aliases.get(normalized)
   if selected not in models:return self.sendj({'type':'error','error':{'type':'invalid_request_error','message':'Model is not available in the current Codex account catalog; update available_models and model_aliases.'}},400)
   try:modes=request_modes(b)
   except InvalidMode as e:return self.sendj({'type':'error','error':{'type':'invalid_request_error','message':str(e)}},400)
   body={'model':selected,'instructions':instructions or 'Be helpful.','input':inp,'tools':tools,'store':False,'stream':True,**modes,**extra,'include':['reasoning.encrypted_content']}
   def send(auth):
    secrets.extend(auth.values())
    return requests.post(UPSTREAM,headers={'Authorization':'Bearer '+auth['access_token'],'ChatGPT-Account-ID':auth['account_id'],'Content-Type':'application/json','Accept':'text/event-stream','originator':'codex_cli_rs','User-Agent':'codex_cli_rs/0.163.0'},json=body,stream=True,timeout=120)
   r,auth=authorized_post(AuthStore(AUTH),send)
   message_id='msg_'+uuid.uuid4().hex
   def event(name,data):
    nonlocal streaming_started
    if not streaming_started:
     self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Connection','close');self.send_header('X-Accel-Buffering','no');self.end_headers()
     streaming_started=True
     start={'type':'message_start','message':{'id':message_id,'type':'message','role':'assistant','content':[],'model':requested,'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':0,'output_tokens':0}}}
     self.wfile.write(('event: message_start\ndata: '+json.dumps(start)+'\n\n').encode())
    self.wfile.write(('event: '+name+'\ndata: '+json.dumps(data)+'\n\n').encode());self.wfile.flush()
   # A proxy or subscription endpoint can close a chunked SSE response before
   # delivering any event. Retry once only before our own SSE headers are sent:
   # no visible text/tool call can then be duplicated, and no client result has
   # been presented as successful. Never retry after the first forwarded event.
   for stream_attempt in range(2):
    if r.status_code!=200:
     try:
      status,error=upstream_error(r,secrets)
      return self.sendj(error,status)
     finally:r.close()
    headers_at=time.monotonic()
    try:
     result,first_at=read_response(r,event if b.get('stream') else None)
     break
    except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError, requests.exceptions.SSLError) as e:
     if streaming_started or stream_attempt:
      raise
     print('upstream_pre_stream_interrupt',type(e).__name__,flush=True)
     r,auth=authorized_post(AuthStore(AUTH),send)
    finally:
     r.close()
   blocks=[]
   for x in result.get('output',[]):
    if x.get('type')=='message':blocks.extend({'type':'text','text':c.get('text','')} for c in x.get('content',[]) if c.get('type')=='output_text')
    elif x.get('type')=='function_call' and not is_token_limit(result):blocks.append({'type':'tool_use','id':x['call_id'],'name':x['name'],'input':json.loads(x['arguments'])})
   reason='max_tokens' if is_token_limit(result) else ('tool_use' if any(x['type']=='tool_use' for x in blocks) else 'end_turn')
   u=result.get('usage',{});cached=u.get('input_tokens_details',{}).get('cached_tokens',0)
   usage={'input_tokens':max(0,u.get('input_tokens',0)-cached),'cache_read_input_tokens':cached,'cache_creation_input_tokens':0,'output_tokens':u.get('output_tokens',0)}
   print(json.dumps({'upstream':'chatgpt-subscription','model':result.get('model'),'requestedModel':requested,'reasoning_effort':modes['reasoning']['effort'],'requested_tier':modes.get('service_tier','default'),'response_tier_echo':result.get('service_tier'),'tier_confirmation':'unconfirmed' if modes.get('service_tier') == 'priority' else 'not_requested','blocks':[x.get('name',x['type']) for x in blocks],'usage':usage,'headers_ms':round((headers_at-started)*1000),'first_content_ms':round((first_at-started)*1000) if first_at else None,'total_ms':round((time.monotonic()-started)*1000)}),flush=True)
   msg={'id':message_id,'type':'message','role':'assistant','content':blocks,'model':requested,'stop_reason':reason,'stop_sequence':None,'usage':usage}
   if not b.get('stream'):return self.sendj(msg)
   event('message_delta',{'type':'message_delta','delta':{'stop_reason':reason,'stop_sequence':None},'usage':usage});event('message_stop',{'type':'message_stop'})
  except AuthError as e:
   self.sendj({'type':'error','error':{'type':'authentication_error' if e.status==401 else 'api_error','message':str(e)}},e.status)
  except (InvalidRequest,InvalidMode,json.JSONDecodeError,KeyError,TypeError,ValueError) as e:
   error={'type':'error','error':{'type':'invalid_request_error','message':str(e) if isinstance(e,(InvalidRequest,InvalidMode)) else 'Malformed request or local configuration'}}
   try:
    if streaming_started:event('error',error)
    else:self.sendj(error,400)
   except (BrokenPipeError,ConnectionResetError):pass
  except Exception as e:
   print('error',type(e).__name__,flush=True)
   detail=safe_detail(str(e),locals().get('secrets',[])) if isinstance(e,ResponseFailed) else type(e).__name__
   try:
    if streaming_started:event('error',{'type':'error','error':{'type':'api_error','message':detail}})
    else:self.sendj({'error':{'type':'api_error','message':detail}},502)
   except:pass
if __name__=='__main__':ThreadingHTTPServer(('127.0.0.1',int(os.environ.get('BRIDGE_PORT','18734'))),H).serve_forever()
