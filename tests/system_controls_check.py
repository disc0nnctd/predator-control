"""HTTP and adapter checks; all operations are mocked, never change either machine."""
import importlib
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import system_controls as C
import server as S

class Controls(unittest.TestCase):
    def test_allowlist_and_confirmation(self):
        invalid = [None, [], {}, {'domain': [], 'action': 'status'},
                   {'domain': 'shell', 'action': 'run'},
                   {'domain': 'fan', 'action': 'status;id'},
                   {'domain': 'fan', 'action': 'on'},
                   {'domain': 'fan', 'action': 'status', 'command': 'id'},
                   {'domain': 'fan', 'action': 'status', 'target': 'id'},
                   {'domain': 'wifi', 'action': 'switch', 'confirm': True, 'wait_seconds': True},
                   {'domain': 'llm', 'action': 'start', 'confirm': True, 'duration_minutes': 241},
                   {'domain': 'llm', 'action': 'start', 'confirm': True, 'target': 'arbitrary.service'},
                   {'domain': 'image-model', 'action': 'set', 'confirm': True},
                   {'domain': 'predator', 'action': 'mode', 'target': 'gaming', 'confirm': True}]
        for body in invalid:
            with self.subTest(body=body), self.assertRaises(C.ControlError): C.validate(body)

    def test_catalog_dispatches_every_action(self):
        control, fan = C.load_handlers()
        with patch.object(control, 'dispatch', return_value='{"ok":true,"message":"checked"}') as dispatch, \
             patch.object(control, 'predator_control', return_value='{"ok":true,"message":"image"}') as image, \
             patch.object(fan, '_handle_fan', return_value='fan status') as fan_call:
            for group in C.GROUPS:
                for spec in group['actions']:
                    target = 'uc-q4_k_m' if group['domain']=='image-model' and spec['action']=='set' else ''
                    body = dict(domain=group['domain'],action=spec['action'],target=target,confirm=True)
                    request, mutates = C.validate(body)
                    self.assertEqual(mutates, spec['mutates'])
                    self.assertTrue(C.execute(request)['ok'])
                    if group['domain']=='fan':fan_call.assert_called_with(spec['action'])
                    elif group['domain']=='image-model': image.assert_called_with('image-model', target=target)
                    else: dispatch.assert_called_with(**request)

    def test_async_serialization_and_failure(self):
        entered, release = threading.Event(), threading.Event()
        def work(_):
            entered.set();release.wait(3);raise RuntimeError('test failure')
        try:
            with patch.object(C,'execute',side_effect=work):
                job=C.submit({'domain':'health','action':'summary'})
                self.assertTrue(entered.wait(1))
                with self.assertRaises(C.ControlError) as error: C.submit({'domain':'health','action':'summary'})
                self.assertEqual(error.exception.code,409)
                release.set()
                for _ in range(100):
                    result=next(x for x in C.jobs()['jobs'] if x['id']==job['id'])
                    if not result['running']:break
                    time.sleep(.01)
                self.assertFalse(result['running']);self.assertFalse(result['result']['ok'])
        finally:release.set()

class HTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_token=S.TOKEN;S.TOKEN='test-system-controls-token'
        cls.http=S.ThreadingHTTPServer(('127.0.0.1',0),S.H)
        cls.base='http://127.0.0.1:'+str(cls.http.server_port)
        threading.Thread(target=cls.http.serve_forever,daemon=True).start()
    @classmethod
    def tearDownClass(cls):cls.http.shutdown();cls.http.server_close();S.TOKEN=cls.old_token
    def request(self,path,body=None,auth=True):
        req=Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,
                    headers={'X-Token':S.TOKEN if auth else '', 'Content-Type':'application/json'})
        try:
            with urlopen(req,timeout=3) as response:return response.status,json.load(response)
        except HTTPError as error:return error.code,json.load(error)
    def test_authentication(self):
        with patch.object(C,'submit') as submit:
            for route in ['capabilities','jobs','monitor']:
                self.assertEqual(self.request('/api/system/'+route,auth=False)[0],401)
            self.assertEqual(self.request('/api/system/run',{'domain':'fan','action':'on','confirm':True},False)[0],401)
            submit.assert_not_called()
    def test_confirm_rejected_before_submission(self):
        with patch.object(C,'submit') as submit:
            self.assertEqual(self.request('/api/system/run',{'domain':'fan','action':'on'})[0],400)
            submit.assert_not_called()
    def test_background_request_accepted_and_results_visible(self):
        with patch.object(C,'execute',return_value={'ok':True,'message':'mock status'}),patch.object(S,'log'):
            status,job=self.request('/api/system/run',{'domain':'fan','action':'status'})
            self.assertEqual(status,202)
            for _ in range(50):
                status,jobs=self.request('/api/system/jobs')
                result=next(j for j in jobs['jobs'] if j['id']==job['id'])
                if not result['running']:break
                time.sleep(.01)
            self.assertEqual(result['result']['message'],'mock status')
    def test_predator_busy_protection(self):
        with patch.object(S,'status',return_value={'busy':{'busy':True}}),patch.object(C,'submit') as submit:
            code,_=self.request('/api/system/run',{'domain':'predator','action':'sleep','confirm':True})
            self.assertEqual(code,409);submit.assert_not_called()
    def test_catalog_and_monitor(self):
        self.assertEqual(self.request('/api/system/capabilities')[0],200)
        self.assertEqual(self.request('/api/system/monitor')[0],200)

if __name__=='__main__':unittest.main(verbosity=2)
