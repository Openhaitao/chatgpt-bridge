"""Offline refresh tests: temporary fake credentials and loopback servers only."""
import base64
import concurrent.futures
from datetime import datetime, timezone
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import requests
import codex_auth as auth
from test_bridge import serving
from http.server import BaseHTTPRequestHandler


def jwt(**claims):
    return 'fake.' + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=') + '.fake'


class MockToken(BaseHTTPRequestHandler):
    calls = []
    status = 200
    reply = {}
    callback = None
    def log_message(self, *args):
        pass
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        type(self).calls.append(payload)
        if type(self).callback:
            type(self).callback()
        self.send_response(type(self).status)
        self.end_headers()
        self.wfile.write(json.dumps(type(self).reply).encode())


def process_refresh(path, endpoint, document):
    return auth.AuthStore(path, endpoint=endpoint).refresh(document, 'unauthorized_recovery')['tokens']['access_token']


class Response:
    def __init__(self, status):
        self.status_code = status
        self.closed = False
    def close(self):
        self.closed = True


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'auth.json'
        self.document = {'auth_mode':'chatgpt', 'custom':{'keep':True}, 'tokens':{
            'access_token':'fake-old', 'refresh_token':'fake-refresh', 'id_token':'fake-id',
            'account_id':'fake-account', 'custom':'keep'}}
        self.write(self.document)
        MockToken.calls = []
        MockToken.status = 200
        MockToken.reply = {'access_token':'fake-new', 'refresh_token':'fake-rotated', 'id_token':'fake-new-id'}
        MockToken.callback = None

    def write(self, document):
        self.path.write_text(json.dumps(document))

    def test_expiry(self):
        for exp, expected in [(1000,'proactive_expiry'),(1400,None),(True,None),('900',None),(float('inf'),None)]:
            self.document['tokens']['access_token'] = jwt(exp=exp)
            self.assertEqual(auth.refresh_reason(self.document, 1000), expected)
        self.document['tokens']['access_token'] = 'opaque'
        self.assertIsNone(auth.refresh_reason(self.document, 1000))
        self.document['last_refresh'] = '2020-01-01T00:00:00Z'
        self.assertEqual(auth.refresh_reason(self.document), 'scheduled')
        self.document['last_refresh'] = 'invalid'
        self.assertIsNone(auth.refresh_reason(self.document))
        self.document['last_refresh'] = '2020-01-01'  # timezone-less is not reliable
        self.assertIsNone(auth.refresh_reason(self.document))

    def test_protocol_rotation_atomic_permissions(self):
        with serving(MockToken) as endpoint:
            result = auth.AuthStore(self.path, endpoint=endpoint).refresh(self.document, 'unauthorized_recovery')
        self.assertEqual(MockToken.calls, [{'grant_type':'refresh_token','client_id':auth.CLIENT_ID,
                                          'refresh_token':'fake-refresh','refresh_reason':'unauthorized_recovery'}])
        self.assertEqual(result['tokens']['refresh_token'], 'fake-rotated')
        self.assertEqual(result['tokens']['account_id'], 'fake-account')
        self.assertEqual(result['tokens']['custom'], 'keep')
        self.assertEqual(result['custom'], {'keep':True})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(str(self.path)+'.bridge-refresh.lock').stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.path.read_text()), result)
        self.assertEqual(list(self.path.parent.glob('.bridge-auth-*')), [])

    def test_optional_rotation_fields(self):
        MockToken.reply = {'access_token':'fake-new'}
        with serving(MockToken) as endpoint:
            result = auth.AuthStore(self.path, endpoint=endpoint).refresh(self.document, 'scheduled')
        self.assertEqual(result['tokens']['refresh_token'], 'fake-refresh')
        self.assertEqual(result['tokens']['id_token'], 'fake-id')

    def test_threads_single_refresh(self):
        with serving(MockToken) as endpoint:
            store = auth.AuthStore(self.path, endpoint=endpoint)
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: store.refresh(self.document, 'scheduled'), range(8)))
        self.assertEqual(len(MockToken.calls), 1)
        self.assertTrue(all(r['tokens']['access_token']=='fake-new' for r in results))

    def test_processes_single_refresh(self):
        with serving(MockToken) as endpoint:
            with concurrent.futures.ProcessPoolExecutor(max_workers=3, mp_context=multiprocessing.get_context('spawn')) as pool:
                futures = [pool.submit(process_refresh, self.path, endpoint, self.document) for _ in range(3)]
                self.assertEqual([f.result(timeout=15) for f in futures], ['fake-new']*3)
        self.assertEqual(len(MockToken.calls), 1)

    def test_external_rotation_before_refresh(self):
        changed = json.loads(json.dumps(self.document))
        changed['tokens']['refresh_token'] = 'external-refresh'
        self.write(changed)
        with patch('requests.post') as post:
            self.assertEqual(auth.AuthStore(self.path).refresh(self.document, 'scheduled'), changed)
            post.assert_not_called()

    def test_external_rotation_during_refresh_and_metadata_merge(self):
        for rotation in (False, True):
            self.write(self.document)
            changed = json.loads(json.dumps(self.document))
            changed['custom'] = 'externally changed'
            if rotation:
                changed['tokens']['refresh_token'] = 'external-refresh'
                changed['tokens']['access_token'] = 'external-access'
            MockToken.callback = lambda: self.write(changed)
            with serving(MockToken) as endpoint:
                result = auth.AuthStore(self.path, endpoint=endpoint).refresh(self.document, 'scheduled')
            self.assertEqual(result['custom'], 'externally changed')
            self.assertEqual(result['tokens']['access_token'], 'external-access' if rotation else 'fake-new')

    def test_account_switch_and_token_mismatch(self):
        changed = json.loads(json.dumps(self.document))
        changed['tokens']['account_id'] = 'other-account'
        MockToken.callback = lambda: self.write(changed)
        with serving(MockToken) as endpoint:
            with self.assertRaisesRegex(auth.AuthError, 'account changed'):
                auth.AuthStore(self.path, endpoint=endpoint).refresh(self.document, 'scheduled')
        self.assertEqual(json.loads(self.path.read_text()), changed)
        self.write(self.document)
        MockToken.callback = None
        MockToken.reply = {'access_token':jwt(**{'https://api.openai.com/auth':{'chatgpt_account_id':'other'}})}
        with serving(MockToken) as endpoint:
            with self.assertRaisesRegex(auth.AuthError, 'different account'):
                auth.AuthStore(self.path, endpoint=endpoint).refresh(self.document, 'scheduled')
        self.assertEqual(json.loads(self.path.read_text()), self.document)

    def test_permanent_errors_sanitized_cached_and_reset(self):
        for code in ('refresh_token_expired','refresh_token_reused','refresh_token_invalidated','invalid_grant'):
            with self.subTest(code=code):
                auth._FAILURES.clear()
                self.write(self.document)
                MockToken.status = 400
                MockToken.reply = {'error':{'code':code,'message':'secret fake-refresh'}}
                before = len(MockToken.calls)
                with serving(MockToken) as endpoint:
                    store = auth.AuthStore(self.path, endpoint=endpoint)
                    for _ in range(2):
                        with self.assertRaises(auth.AuthError) as caught:
                            store.refresh(self.document, 'scheduled')
                        self.assertEqual(caught.exception.status, 401)
                        self.assertIn('codex login', str(caught.exception))
                        self.assertNotIn('secret', str(caught.exception))
                        self.assertNotIn('fake-refresh', str(caught.exception))
                self.assertEqual(len(MockToken.calls)-before, 1)
                self.assertEqual(json.loads(self.path.read_text()), self.document)
        self.document['tokens']['refresh_token'] = 'new-login'
        self.write(self.document)
        MockToken.status = 200
        MockToken.reply = {'access_token':'fake-new'}
        with serving(MockToken) as endpoint:
            auth.AuthStore(self.path, endpoint=endpoint).refresh(self.document, 'scheduled')

    def test_transient_and_malformed_response_preserve_file(self):
        for status, reply in [(503,{'error':'secret'}),(302,{}),(200,{}),(200,{'access_token':42})]:
            MockToken.status, MockToken.reply = status, reply
            with serving(MockToken) as endpoint:
                with self.assertRaises(auth.AuthError) as caught:
                    auth.AuthStore(self.path, endpoint=endpoint).refresh(self.document, 'scheduled')
                self.assertEqual(caught.exception.status,503)
            self.assertEqual(json.loads(self.path.read_text()), self.document)

    def test_transport_timeout_and_no_redirect(self):
        def post(*args, **kwargs):
            self.assertEqual(kwargs['timeout'], (5,15))
            self.assertFalse(kwargs['allow_redirects'])
            raise requests.Timeout('secret-token')
        with self.assertRaises(auth.AuthError) as caught:
            auth.AuthStore(self.path, post=post).refresh(self.document, 'scheduled')
        self.assertEqual(caught.exception.status, 503)
        self.assertNotIn('secret-token', str(caught.exception))
        self.assertEqual(json.loads(self.path.read_text()), self.document)

    def test_save_compare_and_write_failure(self):
        store = auth.AuthStore(self.path)
        _, snapshot = store._read()
        self.path.write_text(json.dumps(dict(self.document, external=True)))
        with self.assertRaisesRegex(auth.AuthError, 'changed while saving'):
            store._save(self.document, snapshot)
        self.assertTrue(json.loads(self.path.read_text())['external'])
        self.assertEqual(list(self.path.parent.glob('.bridge-auth-*')), [])
        self.write(self.document)
        with serving(MockToken) as endpoint, patch.object(auth.os,'replace',side_effect=OSError('secret')):
            with self.assertRaises(auth.AuthError) as caught:
                auth.AuthStore(self.path, endpoint=endpoint).refresh(self.document,'scheduled')
        self.assertNotIn('secret',str(caught.exception))
        self.assertEqual(json.loads(self.path.read_text()), self.document)

    def test_bad_file_symlink_and_missing_refresh(self):
        self.path.write_text('{}')
        with self.assertRaises(auth.AuthError): auth.AuthStore(self.path).load()
        self.path.unlink()
        self.path.symlink_to(Path(self.directory.name)/'missing')
        with self.assertRaises(auth.AuthError): auth.AuthStore(self.path).load()
        self.path.unlink()
        del self.document['tokens']['refresh_token']
        self.write(self.document)
        with self.assertRaisesRegex(auth.AuthError, 'no refresh token'):
            auth.AuthStore(self.path).refresh(self.document, 'scheduled')

    def test_lock_timeout(self):
        import fcntl
        with open(str(self.path)+'.bridge-refresh.lock','w') as file:
            fcntl.flock(file, fcntl.LOCK_EX)
            with patch.object(auth,'LOCK_TIMEOUT',0):
                with self.assertRaisesRegex(auth.AuthError,'busy'):
                    auth.AuthStore(self.path).refresh(self.document,'scheduled')

    def test_401_one_refresh_one_retry_and_no_loop(self):
        for final in (200,401):
            self.write(self.document)
            calls=[]
            first=Response(401)
            def send(tokens):
                calls.append(tokens['access_token'])
                return first if len(calls)==1 else Response(final)
            with serving(MockToken) as endpoint:
                store=auth.AuthStore(self.path,endpoint=endpoint)
                if final==200:
                    response,_=auth.authorized_post(store,send)
                    self.assertEqual(response.status_code,200)
                else:
                    with self.assertRaisesRegex(auth.AuthError,'automatic retries stopped'):
                        auth.authorized_post(store,send)
            self.assertTrue(first.closed)
            self.assertEqual(calls,['fake-old','fake-new'])

    def test_401_reload_external_credentials_without_refresh(self):
        store=auth.AuthStore(self.path)
        calls=[]
        def send(tokens):
            calls.append(tokens['access_token'])
            if len(calls)==1:
                latest=store.load()
                latest['tokens']['access_token']='external-access'
                self.write(latest)
                return Response(401)
            return Response(200)
        with patch.object(store,'refresh') as refresh:
            auth.authorized_post(store,send)
            refresh.assert_not_called()
        self.assertEqual(calls,['fake-old','external-access'])

    def test_proactive_only_one_refresh_even_on_401(self):
        self.document['tokens']['access_token']=jwt(exp=1)
        self.write(self.document)
        calls=[]
        def send(tokens):
            calls.append(tokens)
            return Response(401)
        with serving(MockToken) as endpoint:
            with self.assertRaisesRegex(auth.AuthError,'still unauthorized'):
                auth.authorized_post(auth.AuthStore(self.path,endpoint=endpoint),send)
        self.assertEqual(len(calls),1)
        self.assertEqual(len(MockToken.calls),1)

    def test_gateway_refresh_before_json_and_sse(self):
        import gateway
        from test_bridge import Fake
        config = Path(self.directory.name)/'config.json'
        config.write_text(json.dumps({'local_token':'fake-local'}))
        Fake.status = 200
        Fake.events = None
        for stream in (False, True):
            self.document['tokens']['access_token'] = jwt(exp=1)
            self.write(self.document)
            with serving(MockToken) as endpoint, serving(Fake) as upstream:
                store = auth.AuthStore(self.path, endpoint=endpoint)
                with patch.object(gateway,'CONFIG',config), patch.object(gateway,'AUTH',self.path), patch.object(gateway,'UPSTREAM',upstream), patch.object(gateway,'AuthStore',return_value=store), serving(gateway.H) as url:
                    response = requests.post(url+'/v1/messages',headers={'Authorization':'Bearer fake-local'},json={'model':'gpt-6-astra','messages':[{'role':'user','content':'fake'}],'stream':stream},timeout=10)
                    self.assertEqual(response.status_code,200)
                    if stream:
                        self.assertIn('message_stop',response.text)
                    else:
                        self.assertEqual(response.json()['content'][0]['text'],'OK')
            self.assertEqual(json.loads(self.path.read_text())['tokens']['refresh_token'],'fake-rotated')
        self.assertEqual(len(MockToken.calls),2)

    def test_response_size_and_wall_time_limits(self):
        class FakeResponse:
            status_code = 200
            closed = False
            def iter_content(self, chunk_size):
                yield b'x'
                yield b'x'
            def close(self):
                self.closed = True
        for limit in ('MAX_RESPONSE','TRANSPORT_DEADLINE'):
            response = FakeResponse()
            with patch.object(auth,limit,0):
                with self.assertRaisesRegex(auth.AuthError,'safety limits'):
                    auth.AuthStore(self.path,post=lambda *a,**k:response).refresh(self.document,'scheduled')
            self.assertTrue(response.closed)
            self.assertEqual(json.loads(self.path.read_text()),self.document)

    def test_no_refresh_on_other_upstream_status(self):
        for status in (200,400,403,429,500):
            store=auth.AuthStore(self.path)
            with patch.object(store,'refresh') as refresh:
                response,_=auth.authorized_post(store, lambda _:Response(status))
                self.assertEqual(response.status_code,status)
                refresh.assert_not_called()


if __name__ == '__main__':
    unittest.main()
