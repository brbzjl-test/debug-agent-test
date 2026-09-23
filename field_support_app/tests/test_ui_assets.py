import sys
import unittest
import urllib.request
from pathlib import Path


ROOT = Path(__file__).parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from field_support_agent.ui.preview import ASSET_DIR, preview_server


class UiAssetsTest(unittest.TestCase):
    def test_required_assets_exist_and_are_local(self):
        for name in ("index.html", "float.html", "styles.css", "app.js"):
            path = ASSET_DIR / name
            self.assertTrue(path.is_file(), name)
            self.assertGreater(path.stat().st_size, 100)

        index = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="styles.css"', index)
        self.assertIn('src="app.js"', index)
        self.assertNotIn("https://", index)
        self.assertNotIn("http://", index)

    def test_web_ui_contains_required_pages_and_states(self):
        index = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
        script = (ASSET_DIR / "app.js").read_text(encoding="utf-8")
        for screen in ('data-screen="init"', 'data-screen="history"', 'data-screen="detail"', 'data-screen="chat"'):
            self.assertIn(screen, index)
        for label in ("AI 正在分析", "工程师正在处理中", "待验证", "问题已解决"):
            self.assertIn(label, script)
        self.assertIn('id="business-status-list"', index)
        self.assertIn("businessStatus()", script)
        self.assertIn("'/business-status'", script)
        self.assertIn("个实例 · PID", script)
        self.assertIn("运行时长 ${duration}", script)

    def test_settings_page_covers_business_codex_and_feishu(self):
        index = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
        script = (ASSET_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn('data-screen="settings"', index)
        for field in ("repository-settings", "business-log-paths", "codex-binary", "codex-model", "codex-effort", "device-name", "feishu-app-id", "feishu-app-secret", "feishu-connection-mode", "lark-cli-binary", "lark-profile", "management-password-setting"):
            self.assertIn('id="{}"'.format(field), index)
        self.assertIn("verifyFeishu", script)
        self.assertIn("saveSettings", script)
        self.assertIn('<option value="none">none</option>', index)
        self.assertIn('<option value="">跟随 Codex 默认</option>', index)

    def test_startup_setting_is_a_next_boot_toggle(self):
        index = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
        script = (ASSET_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="startup-enabled" type="checkbox"', index)
        self.assertIn("下次开机生效", index)
        self.assertIn("getStartup()", script)
        self.assertIn("setStartup(enabled)", script)
        self.assertIn("addEventListener('change', changeStartup)", script)

    def test_history_recurrence_allocates_sub_id_and_blank_chat(self):
        index = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
        script = (ASSET_DIR / "app.js").read_text(encoding="utf-8")
        self.assertNotIn('id="history-button"', index)
        self.assertIn('id="detail-reopen-button"', index)
        self.assertIn("openIssueDetail(issue)", script)
        self.assertIn("api.timeline(issue.id)", script)
        self.assertIn("padStart(3, '0')", script)
        self.assertIn("state.messages = []", script)
        self.assertIn("createSubIssue", script)
        self.assertIn("本次记录独立保存", script)
        self.assertIn("if (issue.status !== 'closed')", script)
        self.assertIn("await resumeIssueChat(issue)", script)
        self.assertIn("issue.status !== 'closed'", script)
        self.assertIn("state.screen === 'chat'", script)

    def test_history_management_mode_uses_checkboxes_and_one_delete_action(self):
        index = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
        script = (ASSET_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="management-mode-button"', index)
        self.assertIn('id="delete-selected-button"', index)
        self.assertEqual(1, index.count('id="delete-selected-button"'))
        self.assertIn("checkbox.type = 'checkbox'", script)
        self.assertIn("selectedIssueIds", script)
        self.assertIn("api.deleteIssues(selected", script)

    def test_human_handoff_locks_local_composer(self):
        script = (ASSET_DIR / "app.js").read_text(encoding="utf-8")
        styles = (ASSET_DIR / "styles.css").read_text(encoding="utf-8")
        self.assertIn("['analyzing', 'human', 'pending', 'failure_report', 'closed'].includes(state.phase)", script)
        self.assertIn("messageInput.disabled = locked", script)
        self.assertIn("requestHandoff", script)
        self.assertIn("feishu_setup", script)
        self.assertIn("打开飞书设置", script)
        self.assertIn("AI 正在分析，请稍候", script)
        self.assertIn("工程师正在处理中，暂时无法输入", script)
        self.assertIn('id="report-unsolved"', script)
        self.assertIn("reportVerificationFailure", script)
        self.assertIn("填写未解决现象", script)
        self.assertIn("现场验证方法", script)
        self.assertIn("latestSolution.verification_method", script)
        self.assertIn("state.current.verificationMethod = solution.verification_method", script)
        self.assertNotIn("现场状态已经保存。分析完成后会在这里给出结论", script)
        self.assertNotIn("问题和现场状态已发送到飞书", script)
        self.assertIn(".send-button:disabled", styles)

    def test_ai_followup_uses_composer_instead_of_duplicate_button(self):
        script = (ASSET_DIR / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("继续和 AI 沟通", script)
        self.assertNotIn("id=\"continue-ai\"", script)
        self.assertIn("继续补充现象或转给工程师处理", script)
        self.assertIn('id="ai-solved"', script)
        self.assertIn("confirmAiResolution", script)

    def test_chat_enter_sends_and_shift_enter_keeps_newline(self):
        script = (ASSET_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("messageInput.addEventListener('keydown', submitComposerOnEnter)", script)
        self.assertIn("event.key !== 'Enter'", script)
        self.assertIn("event.shiftKey", script)
        self.assertIn("event.isComposing", script)
        self.assertIn("composer.requestSubmit()", script)

    def test_chat_keeps_headers_and_composer_outside_scroll_region(self):
        index = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
        styles = (ASSET_DIR / "styles.css").read_text(encoding="utf-8")
        chat_start = index.index('id="chat-screen"')
        header = index.index('class="issue-header"', chat_start)
        scroll_start = index.index('id="chat-scroll"', chat_start)
        scroll_end = index.index('</div>\n      <form class="composer"', scroll_start)
        composer = index.index('id="composer"', scroll_end)
        self.assertLess(header, scroll_start)
        self.assertLess(scroll_start, scroll_end)
        self.assertLess(scroll_end, composer)
        self.assertIn(".chat-scroll { flex: 1; min-height: 0; overflow-y: auto", styles)
        self.assertIn("html, body { height: 100%; margin: 0; overflow: hidden; }", styles)
        self.assertIn(".composer { flex: 0 0 auto", styles)

    def test_analysis_locks_composer_before_request(self):
        script = (ASSET_DIR / "app.js").read_text(encoding="utf-8")
        phase_position = script.index("state.phase = 'analyzing';")
        request_position = script.index("await api.appendMessage", phase_position)
        self.assertLess(phase_position, request_position)
        self.assertIn("!['ready', 'result'].includes(state.phase)", script)

    def test_desktop_shell_avoids_keyboard_focus_and_shortcuts(self):
        shell = (ASSET_DIR.parent / "shell.py").read_text(encoding="utf-8")
        self.assertIn("WindowDoesNotAcceptFocus", shell)
        self.assertIn("WA_ShowWithoutActivating", shell)
        self.assertNotIn("QShortcut", shell)
        self.assertNotIn("registerHotKey", shell)

    def test_closing_chat_keeps_floating_launcher_alive(self):
        shell = (ASSET_DIR.parent / "shell.py").read_text(encoding="utf-8")
        self.assertIn("setQuitOnLastWindowClosed(False)", shell)
        self.assertIn("def closeEvent", shell)
        self.assertIn("event.ignore()", shell)
        self.assertIn("self.hide()", shell)

    def test_floating_launcher_can_move_and_quit(self):
        shell = (ASSET_DIR.parent / "shell.py").read_text(encoding="utf-8")
        floating = (ASSET_DIR / "float.html").read_text(encoding="utf-8")
        self.assertIn("startSystemMove()", shell)
        self.assertIn("def quitApp", shell)
        self.assertIn('id="floating-close"', floating)
        self.assertIn("desktopBridge.startDrag()", floating)
        self.assertIn("desktopBridge.quitApp()", floating)

    def test_preview_server_serves_the_web_ui(self):
        with preview_server() as (_, url):
            with urllib.request.urlopen(url, timeout=2) as response:
                body = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("现场调试助手", body)


if __name__ == "__main__":
    unittest.main()
