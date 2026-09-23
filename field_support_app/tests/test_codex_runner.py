import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from field_support_agent.analysis.codex_runner import ReadOnlyCodexRunner, SecurityViolation
from field_support_agent.analysis.streaming import AppServerRequestError


class ReadOnlyCodexRunnerTest(unittest.TestCase):
    def test_busy_session_is_reported_without_replacing_thread_in_both_transports(self):
        for streaming in (True, False):
            with self.subTest(streaming=streaming), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                repo = root / 'repo'
                repo.mkdir()
                runner = ReadOnlyCodexRunner([repo], root / 'state', codex_binary='missing-codex')
                error = 'thread thread-original already has an active writer'
                with patch.object(runner, '_has_external_rules', return_value=False), \
                        patch.object(runner, '_fingerprint', return_value='same'), \
                        patch('field_support_agent.analysis.codex_runner.AppServerStream.run',
                              side_effect=AppServerRequestError('thread/resume', error)) as stream, \
                        patch('field_support_agent.analysis.codex_runner.subprocess.run',
                              return_value=SimpleNamespace(returncode=1, stdout='', stderr=error)) as execute:
                    result = runner._run('ISS-1', 'prompt', 'thread-original',
                                         (lambda text: None) if streaming else None)
                self.assertFalse(result.ok)
                self.assertTrue(result.session_busy)
                self.assertFalse(result.session_missing)
                self.assertEqual('thread-original', result.thread_id)
                self.assertEqual(int(streaming), stream.call_count)
                self.assertEqual(int(not streaming), execute.call_count)
                saved = json.loads((root / 'state/issues/ISS-1/analysis/latest.json').read_text())
                self.assertTrue(saved['session_busy'])
                self.assertEqual('thread-original', saved['thread_id'])

    def test_prompt_contains_hard_read_only_rule(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            manifest = root / "manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            runner = ReadOnlyCodexRunner([repo], root / "state", codex_binary="missing-codex")
            prompt = runner._build_prompt("ISS-1", "请修复", manifest)
            self.assertIn("严禁修改", prompt)
            self.assertIn(str(repo), prompt)
            self.assertIn("非技术现场人员", prompt)
            self.assertIn("最多 5 步", prompt)
            self.assertIn("完成后告诉我", prompt)

    def test_detects_repository_change(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            source = repo / "app.py"
            source.write_text("print('a')\n", encoding="utf-8")
            runner = ReadOnlyCodexRunner([repo], root / "state", codex_binary="missing-codex")
            before = {str(repo): runner._fingerprint(repo)}
            source.write_text("print('b')\n", encoding="utf-8")
            with self.assertRaises(SecurityViolation):
                runner._assert_unchanged(before)

    def test_command_policy_is_fail_closed(self):
        source = Path(__file__).parents[1] / "src/field_support_agent/analysis/codex_runner.py"
        text = source.read_text(encoding="utf-8")
        for value in ('"never"', '"read-only"', '"--ignore-rules"', '"mcp_servers={}"', 'network_access="disabled"'):
            self.assertIn(value, text)
        self.assertNotIn('"--ephemeral"', text)
        self.assertNotIn('"workspace-write"', text)
        self.assertNotIn('"danger-full-access"', text)

    def test_runtime_model_and_effort_do_not_change_read_only_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            runner = ReadOnlyCodexRunner(
                [repo],
                root / "state",
                codex_binary="/opt/codex",
                codex_home=root / "codex-home",
                model="gpt-test",
                reasoning_effort="high",
            )
            command = runner._command()
            self.assertEqual("gpt-test", command[command.index("--model") + 1])
            self.assertIn('model_reasoning_effort="high"', command)
            self.assertEqual("read-only", command[command.index("--sandbox") + 1])
            self.assertEqual("never", command[command.index("-a") + 1])
            self.assertEqual(str((root / "codex-home").resolve()), runner._minimal_environment()["CODEX_HOME"])

            resume = runner._command("thread-123")
            self.assertIn("resume", resume)
            self.assertIn("thread-123", resume)
            self.assertEqual("read-only", resume[resume.index("--sandbox") + 1])

    def test_unspecified_model_and_effort_inherit_codex_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            runner = ReadOnlyCodexRunner([repo], Path(temp) / "state", model="", reasoning_effort="")
            command = runner._command()
            self.assertNotIn("--model", command)
            self.assertFalse(any(value.startswith("model_reasoning_effort=") for value in command))
            self.assertIn("read-only", command)
            self.assertIn("mcp_servers={}", command)

    def test_followup_marks_recurrence_and_keeps_reply_simple(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            manifest = root / "manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            runner = ReadOnlyCodexRunner([repo], root / "state", codex_binary="missing-codex")
            prompt = runner._build_followup_prompt(
                "ISS-1-S001", "ISS-1", "再次没有反应", manifest, recurrence=True
            )
            self.assertIn("新的复发记录", prompt)
            self.assertIn("不得默认", prompt)
            self.assertIn("非技术现场人员", prompt)
            self.assertIn("操作最多 5 步", prompt)

    def test_unknown_reasoning_effort_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            with self.assertRaises(ValueError):
                ReadOnlyCodexRunner([repo], Path(temp) / "state", reasoning_effort="extreme")

    def test_external_rules_retain_ignore_rules_execution_instead_of_streaming(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            home = root / "codex-home"
            rules = home / "rules"
            rules.mkdir(parents=True)
            (rules / "default.rules").write_text('prefix_rule(pattern=["python3"], decision="allow")')
            runner = ReadOnlyCodexRunner([repo], root / "state", codex_home=home)
            completed = SimpleNamespace(returncode=0, stderr="", stdout='{"type":"item.completed","item":{"type":"agent_message","text":"answer"}}\n')
            with patch.object(runner, '_fingerprint', return_value='same'), \
                    patch.object(runner, '_run_streaming') as streaming, \
                    patch('field_support_agent.analysis.codex_runner.subprocess.run', return_value=completed) as run:
                result = runner._run('ISS-1', 'prompt', None, lambda text: None)
            streaming.assert_not_called()
            self.assertIn('--ignore-rules', run.call_args.args[0])
            self.assertEqual('answer', result.response)

    def test_project_rules_are_detected_in_repository_ancestors(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / 'repo'
            repo.mkdir()
            rules = root / '.codex' / 'rules'
            rules.mkdir(parents=True)
            (rules / 'test.rules').write_text('')
            runner = ReadOnlyCodexRunner([repo], root / 'state', codex_home=root / 'codex-home')
            self.assertTrue(runner._has_external_rules())


if __name__ == "__main__":
    unittest.main()
