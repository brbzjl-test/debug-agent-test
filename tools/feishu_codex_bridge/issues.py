"""Explicit incident reports, isolated contexts and a locally enforced lifecycle."""
import json
import time
import uuid

from cards import card

ISSUE_HELP = (
    "请先选择【问题上报】【基于旧问题上报】或【普通聊天】。\n"
    "/report：先建立新 ID，状态 open，再发送现象和日志\n"
    "/sub：选择旧问题，生成 sub-ID 后开始本次上报\n"
    "/sub 问题编号：基于指定旧问题建立 sub-ID\n"
    "/issues：查看问题列表\n/use 问题编号：切换并继续该问题\n"
    "/issue 问题编号：查看问题详情\n/history 问题编号：打开独立完整记录\n"
    "/chat：选择普通聊天\n/new：返回入口重新选择\n"
    "/verify 问题编号 解决方案 | 验证步骤：开发人员提交待验证\n"
    "/confirm 问题编号：本次上报人确认已解决\n/reopen 问题编号：本次上报人反馈验证未通过\n"
    "新的现象必须重新【问题上报】；普通聊天不会自动进入台账。"
)


class IssueMixin:
    def __init__(self, state_dir, config=None):
        super().__init__(state_dir)
        self.config = config or {}
        with self.db:
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(inbox)")}
            for name, definition in (("issue_id", "TEXT"), ("kind", "TEXT NOT NULL DEFAULT 'chat'"),
                                     ("reply_target", "TEXT"), ("topic_id", "TEXT")):
                if name not in columns:
                    self.db.execute("ALTER TABLE inbox ADD COLUMN " + name + " " + definition)
            if "msg_type" not in {row[1] for row in self.db.execute("PRAGMA table_info(outbox)")}:
                self.db.execute("ALTER TABLE outbox ADD COLUMN msg_type TEXT NOT NULL DEFAULT 'text'")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS issues (
                    issue_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, reporter_id TEXT NOT NULL,
                    title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','待验证','已解决')),
                    thread_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    solution TEXT NOT NULL DEFAULT '', verification TEXT NOT NULL DEFAULT '',
                    confirmed_by TEXT, confirmed_at REAL,
                    revision INTEGER NOT NULL DEFAULT 1, synced_revision INTEGER NOT NULL DEFAULT 0,
                    record_id TEXT, sync_error TEXT, retry_at REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS active_issues (session_key TEXT PRIMARY KEY, issue_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS conversation_modes (session_key TEXT PRIMARY KEY, mode TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS issue_entries (
                    entry_id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, kind TEXT NOT NULL,
                    actor TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL,
                    record_id TEXT, retry_at REAL NOT NULL DEFAULT 0, sync_error TEXT
                );
                CREATE TABLE IF NOT EXISTS card_actions (
                    token TEXT PRIMARY KEY, chat_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                    command TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0
                );
            """)
            if "verification_version" not in {row[1] for row in self.db.execute("PRAGMA table_info(issues)")}:
                self.db.execute("ALTER TABLE issues ADD COLUMN verification_version INTEGER NOT NULL DEFAULT 0")
            if "remote_proposal" not in {row[1] for row in self.db.execute("PRAGMA table_info(issues)")}:
                self.db.execute("ALTER TABLE issues ADD COLUMN remote_proposal TEXT")
            for name, definition in (("parent_id", "TEXT"), ("child_count", "INTEGER NOT NULL DEFAULT 0")):
                if name not in {row[1] for row in self.db.execute("PRAGMA table_info(issues)")}:
                    self.db.execute("ALTER TABLE issues ADD COLUMN " + name + " " + definition)
            for name, definition in (
                ("doc_token", "TEXT"), ("doc_url", "TEXT"), ("doc_block_id", "TEXT"),
                ("doc_initialized", "INTEGER NOT NULL DEFAULT 0"),
                ("doc_notified", "INTEGER NOT NULL DEFAULT 0"),
                ("doc_sync_error", "TEXT"), ("doc_retry_at", "REAL NOT NULL DEFAULT 0"),
            ):
                if name not in {row[1] for row in self.db.execute("PRAGMA table_info(issues)")}:
                    self.db.execute("ALTER TABLE issues ADD COLUMN " + name + " " + definition)
            for name, definition in (
                ("doc_synced_at", "REAL"), ("doc_sync_error", "TEXT"),
                ("doc_retry_at", "REAL NOT NULL DEFAULT 0"),
            ):
                if name not in {row[1] for row in self.db.execute("PRAGMA table_info(issue_entries)")}:
                    self.db.execute("ALTER TABLE issue_entries ADD COLUMN " + name + " " + definition)
            if "options_json" not in {row[1] for row in self.db.execute("PRAGMA table_info(card_actions)")}:
                self.db.execute("ALTER TABLE card_actions ADD COLUMN options_json TEXT")
            if "topic_id" not in {row[1] for row in self.db.execute("PRAGMA table_info(card_actions)")}:
                self.db.execute("ALTER TABLE card_actions ADD COLUMN topic_id TEXT")

    def issue(self, issue_id):
        return self.db.execute("SELECT * FROM issues WHERE issue_id=?", (issue_id,)).fetchone()

    def active(self, message):
        return self.db.execute(
            "SELECT i.* FROM active_issues a JOIN issues i ON i.issue_id=a.issue_id WHERE a.session_key=?",
            (self.key(message),),
        ).fetchone()

    def allowed_issue(self, message, issue_id):
        item = self.issue(issue_id)
        if not item:
            raise ValueError("找不到这个问题编号。发送 /issues 查看。")
        if item["reporter_id"] != message["sender_id"] and message["sender_id"] not in self.config.get("engineer_users", []):
            raise ValueError("只能访问自己上报的问题；支持工程师需要配置 engineer_users。")
        return item

    def bind(self, message, issue_id, kind="command"):
        self.db.execute("UPDATE inbox SET issue_id=?,kind=? WHERE message_id=?", (issue_id, kind, message["message_id"]))

    def entry(self, issue_id, entry_id, kind, actor, content):
        self.db.execute("INSERT OR IGNORE INTO issue_entries(entry_id,issue_id,kind,actor,content,created_at) VALUES (?,?,?,?,?,?)",
                        (entry_id, issue_id, kind, actor, content, time.time()))

    def touch(self, issue_id):
        self.db.execute("UPDATE issues SET revision=revision+1,updated_at=? WHERE issue_id=?", (time.time(), issue_id))

    def choose_mode(self, message, mode):
        self.db.execute("DELETE FROM active_issues WHERE session_key=?", (self.key(message),))
        self.db.execute("INSERT OR REPLACE INTO conversation_modes VALUES (?,?)", (self.key(message), mode))

    def chat_selected(self, message):
        row = self.db.execute("SELECT mode FROM conversation_modes WHERE session_key=?", (self.key(message),)).fetchone()
        return bool(row and row[0] == "chat")

    def create_issue(self, message, description="", parent_id=None):
        if parent_id:
            self.allowed_issue(message, parent_id)
            # enqueue already holds SQLite's write transaction: counter allocation
            # and child creation commit together, including callback retries.
            self.db.execute("UPDATE issues SET child_count=child_count+1 WHERE issue_id=?", (parent_id,))
            issue_id = parent_id + "-S{:03d}".format(self.issue(parent_id)["child_count"])
        else:
            issue_id = "ISS-" + time.strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:8].upper()
        now = time.time()
        self.db.execute(
            "INSERT INTO issues(issue_id,chat_id,reporter_id,title,description,created_at,updated_at,parent_id) VALUES (?,?,?,?,?,?,?,?)",
            (issue_id, message["chat_id"], message["sender_id"], description.replace("\n", " ")[:80] or "待补充现象", description, now, now, parent_id),
        )
        self.db.execute("DELETE FROM conversation_modes WHERE session_key=?", (self.key(message),))
        self.db.execute("INSERT OR REPLACE INTO active_issues VALUES (?,?)", (self.key(message), issue_id))
        detail = "显式选择【基于旧问题上报】，父问题 " + parent_id if parent_id else "显式选择【问题上报】"
        self.entry(issue_id, message["message_id"] + ":open", "状态变更", message["sender_id"], detail + "；创建独立记录，状态 open")
        if parent_id:
            parent = self.issue(parent_id)
            reference = {"父问题编号": parent_id, "标题": parent["title"], "当时状态": parent["status"],
                         "旧现象": parent["description"][:4000], "旧解决方案": parent["solution"][:4000],
                         "旧验证步骤": parent["verification"][:2000]}
            self.entry(issue_id, message["message_id"] + ":reference", "旧问题参考", "bridge", json.dumps(reference, ensure_ascii=False))
        return issue_id

    def action_card(self, message, part_key, title, text, actions, *, url=True, url_label="打开问题台账", options=None):
        buttons = []
        for label, command in actions:
            token = uuid.uuid4().hex
            self.db.execute("INSERT INTO card_actions(token,chat_id,owner_id,command,topic_id) VALUES (?,?,?,?,?)",
                            (token, message["chat_id"], message["sender_id"], command, self.topic_id(message)))
            buttons.append((label, token))
        selection = None
        if options:
            token = uuid.uuid4().hex
            self.db.execute("INSERT INTO card_actions(token,chat_id,owner_id,command,options_json,topic_id) VALUES (?,?,?,?,?,?)",
                            (token, message["chat_id"], message["sender_id"], "/sub-select", json.dumps([value for _, value in options]), self.topic_id(message)))
            selection = (token, options)
        target_url = url if isinstance(url, str) else (self.config.get("base") or {}).get("url") if url else None
        payload = card(title, text, buttons, url=target_url, url_label=url_label, selection=selection)
        self._reply(message["message_id"], part_key, json.dumps(payload, ensure_ascii=False))
        self.db.execute("UPDATE outbox SET msg_type='interactive' WHERE message_id=? AND part_key=?", (message["message_id"], part_key))

    def menu(self, message):
        self.action_card(message, "menu", "是否要上报问题？", "新问题先建立 ID；基于旧问题上报先建立 sub-ID，再开始收集本次信息。\n选择普通聊天则不建问题记录。",
                         [("问题上报", "/report"), ("基于旧问题上报", "/sub"), ("普通聊天", "/chat")])

    def select_parent(self, message):
        self.choose_mode(message, "choose")
        rows = self.visible_issues(message)
        self.finish(message, "请选择本次上报关联的旧问题。选定后生成 sub-ID，状态 open。" if rows else "目前没有可选择的旧问题，请先选择【问题上报】建立新 ID。")
        if rows:
            self.action_card(message, "parent-selector", "基于旧问题上报", "选择下面的旧 ID，为本次上报建立独立记录。\n显示最近 30 条；更早的记录可发送 /sub 问题编号。",
                             [("返回入口", "/new")], options=[(r["issue_id"] + " · " + r["status"] + " · " + r["title"][:40], r["issue_id"]) for r in rows])
        else:
            self.menu(message)

    def enqueue_callback(self, event):
        if not isinstance(event, dict) or event.get("type") != "card.action.trigger":
            return False
        try:
            token = json.loads(event.get("action_value", "{}"))["bridge_action"]
        except (ValueError, TypeError, KeyError):
            return False
        action = self.db.execute("SELECT * FROM card_actions WHERE token=?", (token,)).fetchone()
        if not action or action["owner_id"] != event.get("operator_id") or action["chat_id"] != event.get("chat_id"):
            return False
        if not event.get("event_id") or not event.get("message_id"):
            return False
        command = action["command"]
        if command == "/sub-select":
            selected = event.get("option")
            if not isinstance(selected, str) or selected not in json.loads(action["options_json"] or "[]"):
                return False
            command = "/sub " + selected
        synthetic = {
            "type": "im.message.receive_v1", "event_id": event["event_id"],
            "message_id": "card:" + token, "reply_target": event["message_id"],
            "chat_id": event["chat_id"], "sender_id": event["operator_id"],
            "message_type": "text", "content": command, "card_token": token,
            "topic_id": action["topic_id"],
        }
        with self.db:
            if action["used"]:
                return False
            # Every rendered action is single-use, including /report, so double
            # clicks with distinct callback event IDs cannot create two reports.
            accepted = self.enqueue(synthetic)
            if accepted:
                self.db.execute("UPDATE card_actions SET used=1 WHERE token=?", (token,))
            return accepted

    def enqueue(self, event, connection=None):
        with self.db:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO inbox(message_id,chat_id,sender_id,content,event_json,received_at,reply_target,topic_id) VALUES (?,?,?,?,?,?,?,?)",
                (event["message_id"], event["chat_id"], event["sender_id"], event["content"], json.dumps(event, ensure_ascii=False),
                 time.time(), event.get("reply_target", event["message_id"]), self.topic_id(event)),
            )
            if not cur.rowcount:
                return False
            if event.get("message_type") != "text":
                self.finish(event, "当前只支持文字。请发送现象或日志文字。")
                return True
            text = event["content"].strip()
            command, _, argument = text.partition(" ")
            if text == "问题上报" or text == "【问题上报】":
                command = "/report"
            try:
                if command == "/sub" and not argument:
                    self.select_parent(event)
                elif command in ("/report", "/sub"):
                    parent_id = argument.strip() if command == "/sub" else None
                    description = argument if command == "/report" else ""
                    issue_id = self.create_issue(event, description, parent_id)
                    self.bind(event, issue_id)
                    if description:
                        self.queue_question(event, issue_id, description)
                    else:
                        self.finish(event, "问题已创建，状态 open。请发送本次的现象、设备和发生时间。联网后会同步到台账。")
                    self.action_card(event, "created", issue_id, "**open · 本次上报已建立**\n" +
                                     ("父问题：" + parent_id + "\n" if parent_id else "") + "请开始描述本次现象，后续文字归入此 ID。",
                                     [("查看本问题", "/issue " + issue_id), ("返回入口", "/new")])
                elif command in ("/help", "/new") or not text:
                    if command == "/new":
                        self.choose_mode(event, "choose")
                    self.finish(event, ISSUE_HELP)
                    self.menu(event)
                elif command == "/issues":
                    self.finish(event, self.issue_list(event))
                    self.menu(event)
                elif command in ("/use", "/issue", "/history"):
                    current = self.active(event)
                    item = self.allowed_issue(event, argument or (current["issue_id"] if current else ""))
                    self.bind(event, item["issue_id"])
                    if command == "/use":
                        self.db.execute("DELETE FROM conversation_modes WHERE session_key=?", (self.key(event),))
                        self.db.execute("INSERT OR REPLACE INTO active_issues VALUES (?,?)", (self.key(event), item["issue_id"]))
                        self.finish(event, "已切换到本问题，后续追问只使用它的上下文。")
                    elif command == "/issue":
                        self.finish(event, self.issue_detail(item))
                    else:
                        self.finish(event, "独立完整记录：" + item["doc_url"] if item["doc_url"] else "独立完整记录正在创建，请稍后重试。")
                    self.issue_card(event, item)
                elif command == "/chat":
                    self.choose_mode(event, "chat")
                    self.finish(event, "已选择普通聊天，请发送你的问题。需要上报时发送 /new 返回入口。")
                elif command in ("/confirm", "/reopen"):
                    parts = argument.split()
                    item = self.allowed_issue(event, parts[0] if parts else "")
                    if len(parts) > 1 and parts[1] != str(item["verification_version"]):
                        raise ValueError("这个确认按钮对应的方案已过期，请查看本问题的最新卡片后再验证。")
                    self.bind(event, item["issue_id"])
                    self.confirm(event, item, success=command == "/confirm")
                elif command == "/verify":
                    issue_id, _, detail = argument.partition(" ")
                    item = self.allowed_issue(event, issue_id)
                    if event["sender_id"] not in self.config.get("engineer_users", []):
                        raise ValueError("此命令仅供已配置的开发人员使用。现场人员负责最终确认。")
                    solution, separator, verification = detail.partition("|")
                    if not separator or not solution.strip() or not verification.strip():
                        raise ValueError("格式：/verify 问题编号 解决方案 | 验证步骤")
                    self.bind(event, issue_id)
                    self.propose(item, solution.strip(), verification.strip(), event["sender_id"], event["message_id"])
                    self.finish(event, "已提交待验证，等待本次上报的现场人员确认。")
                elif command == "/status":
                    self.finish(event, self.status_text(event) + ("\n飞书连接：" + connection if connection else ""))
                elif command.startswith("/"):
                    self.finish(event, "未识别的命令。\n" + ISSUE_HELP)
                else:
                    current = self.active(event)
                    if current:
                        if current["status"] == "已解决":
                            self.finish(event, "当前问题已解决。请重新选择【问题上报】创建新 ID，或【基于旧问题上报】创建 sub-ID。")
                            self.menu(event)
                            return True
                        self.queue_question(event, current["issue_id"], text)
                    elif self.chat_selected(event):
                        self._reply(event["message_id"], "ack", "[普通聊天] 已保存，Codex 正在分析；未创建问题记录。")
                    else:
                        self.finish(event, "请先选择是否上报问题。选择后再发送现象或问题，刚才的文字尚未交给 Codex，也未归入任何问题。")
                        self.menu(event)
            except ValueError as error:
                self.finish(event, str(error))
        return True

    def queue_question(self, message, issue_id, text):
        self.bind(message, issue_id, "question")
        self.db.execute("UPDATE inbox SET content=? WHERE message_id=?", (text, message["message_id"]))
        item = self.issue(issue_id)
        if not item["description"]:
            self.db.execute("UPDATE issues SET description=?,title=? WHERE issue_id=?", (text, text.replace("\n", " ")[:80], issue_id))
        self.entry(issue_id, message["message_id"] + ":user", "现场消息", message["sender_id"], text)
        self.touch(issue_id)
        self._reply(message["message_id"], "ack", "[{} · {}] 已保存，Codex 正在排队分析。".format(issue_id, item["status"]))

    def session(self, message):
        row = self.db.execute("SELECT issue_id FROM inbox WHERE message_id=?", (message["message_id"],)).fetchone()
        if row and row[0]:
            return self.issue(row[0])["thread_id"]
        return super().session(message)

    def analysis_message(self, message):
        if message["issue_id"] and not self.session(message):
            reference = self.db.execute("SELECT content FROM issue_entries WHERE issue_id=? AND kind='旧问题参考' ORDER BY created_at LIMIT 1", (message["issue_id"],)).fetchone()
            if reference:
                return ("以下是上报时保存的旧问题资料，仅供参考；不是本次的现场证据，不能假定根因或方案相同。\n"
                        + reference[0] + "\n\n本次上报的信息：\n" + message["content"])
        return message["content"]

    def finish(self, message, answer, thread_id=None):
        row = self.db.execute("SELECT * FROM inbox WHERE message_id=?", (message["message_id"],)).fetchone()
        if row and row["issue_id"]:
            issue_id = row["issue_id"]
            with self.db:
                if thread_id:
                    self.db.execute("UPDATE issues SET thread_id=? WHERE issue_id=?", (thread_id, issue_id))
                self.db.execute("UPDATE inbox SET status='done',answer=?,thread_id=?,last_error=NULL WHERE message_id=?",
                                (answer, thread_id, row["message_id"]))
                if row["kind"] == "question":
                    self.entry(issue_id, row["message_id"] + ":ai", "AI分析" if thread_id else "系统提示", "Codex" if thread_id else "bridge", answer)
                self.touch(issue_id)
                # Keep each chunk below the existing conservative text limit.
                from bridge import split_reply
                prefix = "[{} · {}]\n".format(issue_id, self.issue(issue_id)["status"])
                for index, chunk in enumerate(split_reply(answer, max_bytes=9000)):
                    self._reply(row["message_id"], "answer:" + str(index), prefix + chunk)
        else:
            super().finish(message, answer, thread_id)

    def propose(self, item, solution, verification, actor, source):
        item = self.issue(item["issue_id"])
        if item["status"] == "已解决":
            return False
        if not solution.strip() or not verification.strip():
            raise ValueError("解决方案和验证步骤都需要填写，不能把普通回答直接当成解决。")
        with self.db:
            self.db.execute("UPDATE issues SET status='待验证',solution=?,verification=?,verification_version=verification_version+1 WHERE issue_id=?", (solution, verification, item["issue_id"]))
            self.entry(item["issue_id"], source + ":pending", "状态变更", actor, "进入待验证\n解决方案：" + solution + "\n验证步骤：" + verification)
            self.touch(item["issue_id"])
            self.notify_reporter(item["issue_id"], "待验证", "已提供解决方案，请按验证步骤检查后确认。\n" + verification)
        return True

    def confirm(self, message, item, success):
        if message["sender_id"] != item["reporter_id"]:
            raise ValueError("最终验证只能由本次上报的现场人员确认。")
        if item["status"] != "待验证":
            raise ValueError("只有【待验证】的问题才能确认；open 不能直接关闭。")
        target = "已解决" if success else "open"
        with self.db:
            self.db.execute("UPDATE issues SET status=?,confirmed_by=?,confirmed_at=? WHERE issue_id=?",
                            (target, message["sender_id"] if success else None, time.time() if success else None, item["issue_id"]))
            self.entry(item["issue_id"], message["message_id"] + ":confirmation", "现场验证", message["sender_id"], "现场确认已解决" if success else "现场反馈仍有问题，退回 open")
            self.touch(item["issue_id"])
            self.finish(message, "现场确认已记录，状态【已解决】。" if success else "已退回 open，请补充验证结果继续排查。")

    def issue_card(self, message, item, suffix="detail"):
        item = self.issue(item["issue_id"])
        if item["status"] == "待验证" and message["sender_id"] == item["reporter_id"]:
            target = item["issue_id"] + " " + str(item["verification_version"])
            actions = [("已解决", "/confirm " + target), ("仍有问题", "/reopen " + target)]
        else:
            actions = ([] if item["status"] == "已解决" else [("继续本次问题", "/use " + item["issue_id"])])
        actions += [("基于此问题再次上报", "/sub " + item["issue_id"]), ("返回入口", "/new")]
        self.action_card(message, "issue:" + suffix, item["issue_id"], "**当前状态：" + item["status"] + "**\n" +
                         ("请由本次上报的现场人员验证后确认。" if item["status"] == "待验证" else "本卡片的操作只作用于这个问题。"), actions,
                         url=item["doc_url"] or True, url_label="打开完整记录" if item["doc_url"] else "打开问题台账")

    def notify_reporter(self, issue_id, title, text):
        item = self.issue(issue_id)
        message = self.db.execute("SELECT * FROM inbox WHERE issue_id=? AND sender_id=? ORDER BY seq DESC LIMIT 1", (issue_id, item["reporter_id"])).fetchone()
        if message:
            suffix = str(item["revision"])
            self._reply(message["message_id"], "notice:" + suffix, "[{} · {}] {}".format(issue_id, title, text))
            self.issue_card(message, item, suffix)

    def visible_issues(self, message=None):
        query, values = "SELECT * FROM issues", ()
        if message and message["sender_id"] not in self.config.get("engineer_users", []):
            query += " WHERE reporter_id=?"
            values = (message["sender_id"],)
        return self.db.execute(query + " ORDER BY created_at DESC LIMIT 30", values).fetchall()

    def issue_list(self, message=None):
        rows = self.visible_issues(message)
        text = "最近 30 个问题（每次上报独立编号）：\n" + "\n".join("{} | {} | {}".format(r["issue_id"], r["status"], r["title"]) for r in rows) if rows else "还没有正式上报的问题。请选择【问题上报】。"
        if rows:
            text += "\n/use ID 继续本次；/sub ID 基于旧问题再次上报；/issue ID 查看详情；/history ID 打开完整记录。"
        return text

    def issue_detail(self, item):
        entries = self.db.execute("SELECT * FROM issue_entries WHERE issue_id=? ORDER BY created_at DESC,rowid DESC LIMIT 12", (item["issue_id"],)).fetchall()
        lines = [item["title"], "状态：" + item["status"], "现象：" + item["description"],
                 "父问题：" + (item["parent_id"] or "无（新问题）"),
                 "解决方案：" + (item["solution"] or "待补充"), "验证步骤：" + (item["verification"] or "待补充"),
                 "同步：" + ("失败：" + item["sync_error"] if item["sync_error"] else "已同步" if item["record_id"] and item["revision"] == item["synced_revision"] else "等待同步"),
                 "最近 12 条处理记录："]
        lines.extend(time.strftime("%m-%d %H:%M", time.localtime(r["created_at"])) + " " + r["kind"] + "\n" + r["content"][:1800] for r in reversed(entries))
        children = self.db.execute("SELECT issue_id,status FROM issues WHERE parent_id=? ORDER BY created_at", (item["issue_id"],)).fetchall()
        if children:
            lines.append("关联的 sub-ID：\n" + "\n".join(r["issue_id"] + " · " + r["status"] for r in children))
        if (self.config.get("base") or {}).get("url"):
            lines.append("完整台账：" + self.config["base"]["url"])
        if item["doc_url"]:
            lines.append("本问题完整记录：" + item["doc_url"])
        elif self.config.get("issue_docs"):
            lines.append("本问题完整记录：正在创建")
        return "\n\n".join(lines)

    def status_text(self, message=None):
        text = super().status_text(None)
        if message:
            current = self.active(message)
            text += "\n当前问题：" + (current["issue_id"] + " · " + current["status"] if current else "普通聊天（未上报）" if self.chat_selected(message) else "等待选择入口")
        count = self.db.execute("SELECT count(*) FROM issues WHERE revision!=synced_revision").fetchone()[0]
        text += "\n待同步问题：" + str(count)
        if self.config.get("issue_docs"):
            documents = self.db.execute(
                "SELECT count(*) FROM issues WHERE doc_token IS NULL OR doc_initialized=0 OR doc_sync_error IS NOT NULL"
            ).fetchone()[0]
            entries = self.db.execute("SELECT count(*) FROM issue_entries WHERE doc_synced_at IS NULL").fetchone()[0]
            text += "\n待同步独立记录：{} 个问题，{} 条内容".format(documents, entries)
        return text
