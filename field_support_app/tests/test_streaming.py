import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from field_support_agent.analysis import AnalysisResult
from field_support_agent.analysis.streaming import ACTIVITY_STAGES, AppServerRequestError, AppServerStream
from field_support_agent.api.server import LocalAPIServer
from field_support_agent.service import CoreService, RuntimeService
from field_support_agent.storage import CoreDatabase
from test_runtime import FakeCollector, FakeRunner


FAKE_SERVER = r'''
import json, sys, time
from pathlib import Path
mode, release, requests = sys.argv[1:]
def send(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)
def event(method, **params):
    send({'method':method,'params':dict(threadId='thread-stream',turnId='turn-1',**params)})
for line in sys.stdin:
    request = json.loads(line)
    with open(requests, 'a') as log:
        log.write(json.dumps(request)+'\n')
    method = request.get('method')
    if method == 'initialize':
        send({'id':request['id'],'result':{}})
    elif method in ('thread/start','thread/resume'):
        if mode == 'busy':
            send({'id':request['id'],'error':{'message':'thread thread-stream already has an active writer'}})
            continue
        if mode == 'missing':
            send({'id':request['id'],'error':{'message':'session not found'}})
            continue
        send({'id':request['id'],'result':{'thread':{'id':'thread-stream'},
              'sandbox':{'type':'invalid' if mode=='unsafe' else 'readOnly'},'approvalPolicy':'never'}})
    elif method == 'turn/start':
        send({'id':request['id'],'result':{'turn':{'id':'turn-1'}}})
        if mode == 'timeout':
            time.sleep(30)
        if mode == 'disconnect':
            sys.exit(0)
        if mode == 'disconnect_error':
            print('unsupported model or authentication', file=sys.stderr, flush=True)
            sys.exit(1)
        event('item/started',item={'id':'comment','type':'agentMessage','phase':'commentary'})
        event('item/agentMessage/delta',itemId='comment',delta='internal commentary')
        event('item/reasoning/textDelta',delta='internal reasoning')
        event('item/started',item={'id':'cmd','type':'commandExecution','commandActions':[
            {'type':'read','path':'/private/secret/app.py'}]})
        event('item/completed',item={'id':'cmd','type':'commandExecution'})
        event('item/started',item={'id':'log','type':'commandExecution','commandActions':[
            {'type':'read','path':'/private/secret/error.log'}]})
        event('item/commandExecution/outputDelta',delta='sensitive tool output')
        event('item/completed',item={'id':'log','type':'commandExecution'})
        event('error',willRetry=True,error={'message':'private service error'})
        event('item/started',item={'id':'answer','type':'agentMessage','phase':'final_answer'})
        event('item/agentMessage/delta',itemId='answer',delta='请检查')
        deadline = time.monotonic()+2
        while not Path(release).exists() and time.monotonic()<deadline:
            time.sleep(.01)
        if not Path(release).exists():
            sys.exit(2)
        event('item/agentMessage/delta',itemId='answer',delta='连接线。')
        event('item/completed',item={'id':'answer','type':'agentMessage','phase':'final_answer','text':'请检查连接线。'})
        event('turn/completed',turn={'id':'turn-1','status':'completed'})
'''


class AppServerStreamingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / 'server.py'
        self.script.write_text(FAKE_SERVER)
        self.requests = self.root / 'requests.jsonl'
        self.release = self.root / 'release'
        self.activity = []

    def run_server(self, mode='normal', thread_id=None, timeout=3, model='test-model', effort='low'):
        updates = []
        def on_update(text):
            updates.append(text)
            self.release.touch()
        transport = AppServerStream(
            [sys.executable, str(self.script), mode, str(self.release), str(self.requests)],
            os.environ.copy(), timeout,
        )
        result = transport.run('prompt', thread_id, str(self.root), model, effort, on_update, self.activity.append)
        return result, updates

    def test_deltas_arrive_before_completion_and_only_public_text_is_emitted(self):
        result, updates = self.run_server()
        self.assertEqual(['请检查', '请检查连接线。', '请检查连接线。'], updates)
        self.assertEqual('请检查连接线。', result[0])
        requests = [json.loads(line) for line in self.requests.read_text().splitlines()]
        start = next(r for r in requests if r.get('method') == 'thread/start')['params']
        turn = next(r for r in requests if r.get('method') == 'turn/start')['params']
        self.assertEqual('read-only', start['sandbox'])
        self.assertEqual('never', start['approvalPolicy'])
        self.assertEqual({'type':'readOnly'}, turn['sandboxPolicy'])
        self.assertEqual('never', turn['approvalPolicy'])

    def test_progress_uses_observed_actions_without_exposing_raw_text(self):
        self.run_server()
        for stage in ['starting', 'waiting', 'analyzing', 'reading_code', 'reading_logs', 'retrying', 'responding', 'saving']:
            self.assertIn(stage, self.activity)
        self.assertTrue(set(self.activity) <= ACTIVITY_STAGES | {None})

    def test_resume_streams_using_existing_thread(self):
        result, updates = self.run_server(thread_id='thread-stream')
        self.assertEqual('thread-stream', result[2])
        requests = [json.loads(line) for line in self.requests.read_text().splitlines()]
        resume = next(r for r in requests if r.get('method') == 'thread/resume')
        self.assertEqual('thread-stream', resume['params']['threadId'])
        self.assertTrue(updates)

    def test_refuses_unconfirmed_read_only_policy(self):
        with self.assertRaisesRegex(RuntimeError, 'read-only'):
            self.run_server(mode='unsafe')
        self.assertNotIn('turn/start', self.requests.read_text())

    def test_busy_resume_stops_without_retry_or_other_session_operations(self):
        with self.assertRaisesRegex(AppServerRequestError, 'already has an active writer') as error:
            self.run_server(mode='busy', thread_id='thread-stream')
        self.assertEqual('thread/resume', error.exception.method)
        methods = [json.loads(line).get('method') for line in self.requests.read_text().splitlines()]
        self.assertEqual(['initialize', 'initialized', 'thread/resume'], methods)

    def test_timeout_and_disconnect_do_not_return_a_successful_answer(self):
        with self.assertRaises(TimeoutError):
            self.run_server(mode='timeout', timeout=.3)
        with self.assertRaisesRegex(RuntimeError, 'disconnected'):
            self.run_server(mode='disconnect')
        with self.assertRaisesRegex(RuntimeError, 'unsupported model or authentication'):
            self.run_server(mode='disconnect_error')

    def test_unspecified_model_and_effort_are_omitted_from_requests(self):
        self.run_server(model=None, effort='')
        requests = [json.loads(line) for line in self.requests.read_text().splitlines()]
        thread = next(request for request in requests if request.get('method') == 'thread/start')['params']
        turn = next(request for request in requests if request.get('method') == 'turn/start')['params']
        self.assertNotIn('model', thread)
        self.assertNotIn('model', turn)
        self.assertNotIn('effort', turn)
        self.assertEqual({'type': 'readOnly'}, turn['sandboxPolicy'])


class BlockingRunner(FakeRunner):
    def __init__(self, fail=False):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.fail = fail

    def start_conversation(self, issue_id, prompt, manifest, *, on_update=None, on_activity=None):
        on_update('请检查')
        self.started.set()
        if not self.release.wait(3):
            raise TimeoutError('test did not release runner')
        if self.fail:
            raise RuntimeError('interrupted')
        on_update('请检查连接线。')
        return AnalysisResult(issue_id, True, '请检查连接线。', (), thread_id='thread-stream')


class RuntimeStreamingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.db = CoreDatabase(root / 'core.sqlite3')
        self.addCleanup(self.db.close)
        self.runner = BlockingRunner()
        self.runtime = RuntimeService(CoreService(self.db), FakeCollector(root), self.runner)
        self.addCleanup(self.runtime.close)
        self.addCleanup(self.runner.release.set)
        self.issue = self.runtime.core.create_issue('reporter')

    def start(self):
        self.runtime.append_message(self.issue.issue_id, 'reporter', 'reporter', '没有反应')
        self.assertTrue(self.runner.started.wait(2))

    def test_live_text_is_exposed_by_api_then_saved_once(self):
        import urllib.request
        self.start()
        api = LocalAPIServer(self.runtime)
        api.start()
        try:
            url = 'http://{}:{}/v1/issues/{}/timeline'.format(*api.address, self.issue.issue_id)
            request = urllib.request.Request(url, headers={'Authorization':'Bearer '+api.session_token})
            with urllib.request.urlopen(request) as response:
                body = json.load(response)
            self.assertEqual('running', body['analysis']['status'])
            self.assertEqual('请检查', body['analysis']['content'])
            self.assertEqual('responding', body['analysis']['stage'])
            self.assertGreaterEqual(body['analysis']['elapsed_seconds'], 0)
            self.assertGreaterEqual(body['analysis']['idle_seconds'], 0)
            self.assertNotIn('last_activity_at', body['analysis'])
            self.assertEqual(['reporter'], [m['role'] for m in body['messages']])
        finally:
            api.close()
        self.runner.release.set()
        self.runtime.wait_for_idle(3)
        timeline = self.runtime.timeline(self.issue.issue_id)
        self.assertIsNone(timeline['analysis'])
        self.assertEqual(['请检查连接线。'], [m.content for m in timeline['messages'] if m.role=='assistant'])

    def test_handoff_hides_inflight_text_and_does_not_append_late_answer(self):
        self.start()
        self.runtime.request_handoff(self.issue.issue_id, 'reporter')
        self.assertIsNone(self.runtime.timeline(self.issue.issue_id)['analysis'])
        self.runner.release.set()
        self.runtime.wait_for_idle(3)
        self.assertEqual(['reporter'], [m.role for m in self.runtime.timeline(self.issue.issue_id)['messages']])

    def test_failed_stream_replaces_partial_text_with_failure_and_unlocks(self):
        self.runner.fail = True
        self.start()
        with self.assertLogs('field_support_agent.service.runtime', level='ERROR'):
            self.runner.release.set()
            self.runtime.wait_for_idle(3)
        timeline = self.runtime.timeline(self.issue.issue_id)
        self.assertIsNone(timeline['analysis'])
        self.assertIn('本次分析没有完成', timeline['messages'][-1].content)
        self.assertNotIn('请检查', timeline['messages'][-1].content)

    def test_polling_does_not_fake_activity_and_an_observed_event_resets_idle(self):
        self.start()
        progress = self.runtime._analysis_progress[self.issue.issue_id]
        last_activity = progress['last_activity_at']
        # Stay away from the integer-second boundary: float subtraction can
        # otherwise produce 25.999999999 and intermittently floor to 25.
        with patch('field_support_agent.service.runtime.time.monotonic', return_value=last_activity + 26.25):
            first = self.runtime.timeline(self.issue.issue_id)['analysis']
            again = self.runtime.timeline(self.issue.issue_id)['analysis']
            self.assertEqual(26, first['idle_seconds'])
            self.assertEqual(first, again)
            self.runtime._update_activity(self.issue.issue_id, 'reading_code')
            resumed = self.runtime.timeline(self.issue.issue_id)['analysis']
            self.assertEqual('reading_code', resumed['stage'])
            self.assertEqual(0, resumed['idle_seconds'])
            self.assertGreaterEqual(resumed['elapsed_seconds'], 26)
            self.runtime._update_activity(self.issue.issue_id, 'private internal text')
            self.assertEqual(resumed, self.runtime.timeline(self.issue.issue_id)['analysis'])


if __name__ == '__main__':
    unittest.main()
