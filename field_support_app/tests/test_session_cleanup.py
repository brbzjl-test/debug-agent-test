import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from field_support_agent.analysis.session_cleanup import delete_sessions
from field_support_agent.domain import ConflictError
from field_support_agent.service import CoreService, RuntimeService
from field_support_agent.storage import CoreDatabase
from test_runtime import FakeCollector, FakeRunner


SESSION = '00000000-0000-4000-8000-000000000001'
OTHER_SESSION = '00000000-0000-4000-8000-000000000002'
SERVER = r'''
import sys,json,time
for line in sys.stdin:
    request=json.loads(line)
    with open(sys.argv[2], 'a') as f:
        f.write(line)
    if request.get('method') == 'initialize':
        print(json.dumps({'id':request['id'],'result':{}}),flush=True)
    elif request.get('method') == 'thread/delete':
        mode=sys.argv[1]
        if mode=='timeout': time.sleep(30)
        if mode=='missing':
            result={'error':{'code':-32600,'message':'no rollout found for thread id '+request['params']['threadId']}}
        elif mode=='error':
            result={'error':{'code':-32603,'message':'permission denied'}}
        else: result={'result':{}}
        print(json.dumps(dict(id=request['id'],**result)),flush=True)
'''


class CleanupTransportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.script = self.root / 'server.py'
        self.script.write_text(SERVER)
        self.requests = self.root / 'requests.jsonl'

    def run_delete(self, mode, ids, callback, timeout=3):
        delete_sessions([sys.executable, str(self.script), mode, str(self.requests)],
                        os.environ.copy(), ids, callback, timeout)

    def test_deletes_exact_session_ids_once_without_starting_analysis(self):
        deleted = []
        self.run_delete('ok', [SESSION, SESSION], deleted.append)
        self.assertEqual([SESSION], deleted)
        requests = [json.loads(line) for line in self.requests.read_text().splitlines()]
        self.assertEqual(['initialize', 'initialized', 'thread/delete'], [r['method'] for r in requests])
        self.assertEqual({'threadId': SESSION}, requests[-1]['params'])

    def test_missing_session_is_idempotent(self):
        deleted = []
        self.run_delete('missing', [SESSION], deleted.append)
        self.assertEqual([SESSION], deleted)

    def test_error_and_timeout_never_acknowledge_deletion(self):
        deleted = []
        with self.assertRaisesRegex(RuntimeError, 'permission denied'):
            self.run_delete('error', [SESSION], deleted.append)
        with self.assertRaises(TimeoutError):
            self.run_delete('timeout', [SESSION], deleted.append, .2)
        self.assertEqual([], deleted)

    def test_requires_uuid_before_starting_process(self):
        with self.assertRaises(ValueError):
            self.run_delete('ok', ['some-name'], lambda value: None)
        self.assertFalse(self.requests.exists())


class CleanupRunner(FakeRunner):
    def __init__(self):
        super().__init__()
        self.deleted = []
        self.fail_after = None

    def delete_conversations(self, ids, on_deleted):
        for thread_id in ids:
            if self.fail_after is not None and len(self.deleted) >= self.fail_after:
                raise RuntimeError('cleanup failed')
            self.deleted.append(thread_id)
            on_deleted(thread_id)


class RuntimeCleanupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        db = CoreDatabase(self.root / 'core.sqlite3')
        self.addCleanup(db.close)
        self.core = CoreService(db)
        self.runner = CleanupRunner()
        self.runtime = RuntimeService(self.core, FakeCollector(self.root), self.runner)
        self.addCleanup(self.runtime.close)
        self.issue = self.core.create_issue('reporter', 'initial description')
        self.core.save_codex_thread(self.issue.issue_id, SESSION)
        self.other = self.core.create_issue('reporter', 'unrelated')
        self.core.save_codex_thread(self.other.issue_id, OTHER_SESSION)
        self.snapshot = self.root / 'issues' / self.issue.issue_id / 'snapshots' / 'snapshot.json'
        self.snapshot.parent.mkdir(parents=True)
        self.snapshot.write_text('{}')

    def test_root_deletion_cleans_session_and_local_data_but_keeps_unrelated_session(self):
        sub = self.core.create_subissue(self.issue.issue_id, 'reporter', 'recurrence')
        deleted = self.runtime.delete_issues([self.issue.issue_id])
        self.assertEqual({self.issue.issue_id, sub.issue_id}, set(deleted))
        self.assertEqual([SESSION], self.runner.deleted)
        self.assertFalse(self.snapshot.exists())
        self.assertEqual(OTHER_SESSION, self.core.codex_thread(self.other.issue_id))

    def test_subissue_deletion_clears_shared_session_and_rebuilds_from_survivors(self):
        sub = self.core.create_subissue(self.issue.issue_id, 'reporter', 'deleted description')
        self.runtime.delete_issues([sub.issue_id])
        self.assertEqual([SESSION], self.runner.deleted)
        self.assertIsNone(self.core.codex_thread(self.issue.issue_id))
        self.assertTrue(self.snapshot.exists())
        self.runtime.append_message(self.issue.issue_id, 'reporter', 'reporter', 'new description')
        self.runtime.wait_for_idle(3)
        self.assertEqual('start', self.runner.calls[-1][0])
        seed = self.runner.calls[-1][2]
        self.assertIn('initial description', seed)
        self.assertNotIn('deleted description', seed)

    def test_failure_keeps_local_records_and_files_for_retry(self):
        self.runner.fail_after = 0
        with self.assertLogs('field_support_agent.service.runtime', level='WARNING'):
            with self.assertRaisesRegex(ConflictError, '本地问题记录已保留'):
                self.runtime.delete_issues([self.issue.issue_id])
        self.assertIsNotNone(self.core.get_issue(self.issue.issue_id))
        self.assertTrue(self.snapshot.exists())
        self.assertEqual(SESSION, self.core.codex_thread(self.issue.issue_id))
        self.assertNotIn(self.issue.issue_id, self.runtime._deleted_issue_ids)
        self.runner.fail_after = None
        self.runtime.delete_issues([self.issue.issue_id])
        self.assertEqual([SESSION], self.runner.deleted)

    def test_partial_batch_can_retry_without_losing_remaining_session_mapping(self):
        self.runner.fail_after = 1
        with self.assertLogs('field_support_agent.service.runtime', level='WARNING'):
            with self.assertRaises(ConflictError):
                self.runtime.delete_issues([self.issue.issue_id, self.other.issue_id])
        self.assertEqual(2, len(self.core.list_issues()))
        self.assertIsNone(self.core.codex_thread(self.issue.issue_id))
        self.assertEqual(OTHER_SESSION, self.core.codex_thread(self.other.issue_id))
        self.runner.fail_after = None
        self.runtime.delete_issues([self.issue.issue_id, self.other.issue_id])
        self.assertEqual([SESSION, OTHER_SESSION], self.runner.deleted)

    def test_inflight_analysis_blocks_deletion_without_cleaning_anything(self):
        lock = self.runtime._analysis_lock(self.issue.issue_id)
        with lock:
            with self.assertRaisesRegex(ConflictError, '正在分析'):
                self.runtime.delete_issues([self.issue.issue_id])
        self.assertEqual([], self.runner.deleted)
        self.assertTrue(self.snapshot.exists())

    def test_no_runner_cannot_report_false_success(self):
        self.runtime.codex_runner = None
        with self.assertRaisesRegex(ConflictError, '启用 Codex'):
            self.runtime.delete_issues([self.issue.issue_id])
        self.assertTrue(self.snapshot.exists())


if __name__ == '__main__':
    unittest.main()
