"""Durable issue/history synchronization using the configured robot identity."""
import asyncio
import hashlib
import json
from pathlib import Path
import time


def payload(result):
    if result.get("ok") is False or result.get("code", 0) not in (0, None):
        raise RuntimeError("飞书返回错误：" + json.dumps(result, ensure_ascii=False)[:1000])
    # Base v3 also uses `data` for the rows of a record matrix. Only unwrap
    # an actual API envelope, never that matrix itself.
    return result["data"] if isinstance(result.get("data"), dict) else result


def records(result):
    data = payload(result)
    if "record_id_list" in data and "fields" in data and isinstance(data.get("data"), list):
        ids, fields, rows = data["record_id_list"], data["fields"], data["data"]
        if len(ids) != len(rows):
            raise ValueError("飞书记录矩阵的 ID 与行数不一致")
        missing = data.get("record_not_found", [])
        return [{"record_id": rid, "fields": dict(zip(fields, row))}
                for rid, row in zip(ids, rows) if rid not in missing]
    value = data.get("records", data.get("items", []))
    if not isinstance(value, list):
        raise ValueError("飞书记录列表结构无效")
    return value


def record_id(record):
    value = record.get("record_id", record.get("id"))
    if not isinstance(value, str) or not value:
        raise ValueError("飞书未返回 record_id")
    return value


def returned_record_id(data):
    """Some Base v3 upserts report changed fields but omit the record ID."""
    record = data.get("record", data) if isinstance(data, dict) else {}
    value = record.get("record_id", record.get("id")) if isinstance(record, dict) else None
    return value if isinstance(value, str) and value else None


def cell_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("text", value.get("name", value.get("value", ""))))
    if isinstance(value, list):
        return "".join(cell_text(item) for item in value)
    return "" if value is None else str(value)


def date(value):
    # Base configured with Asia/Shanghai; do not depend on the host's timezone.
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(value + 8 * 3600))


class BaseSync:
    def __init__(self, config, store, runner):
        self.config, self.store, self.runner = config, store, runner
        self.base = config.get("base") or {}
        self.checked = False
        self.last_poll = {}

    async def call(self, command, table, *args):
        argv = [self.config["lark_cli"], "--profile", self.config["lark_profile"], "base", command,
                "--as", "bot", "--base-token", self.base["base_token"], "--table-id", table, *args, "--format", "json"]
        return json.loads(await self.runner(argv))

    async def check(self):
        if not all(self.base.get(k) for k in ("base_token", "issues_table_id", "history_table_id", "url")):
            raise ValueError("尚未配置多维表格：base_token / issues_table_id / history_table_id / url")
        required = {
            self.base["issues_table_id"]: {field["name"]: field["type"] for field in json.loads((Path(__file__).parent / "base_fields.json").read_text())},
            self.base["history_table_id"]: {"记录编号": "text", "所属问题": "link", "问题编号": "text", "类型": "text", "参与者": "text", "时间": "datetime", "内容": "text"},
        }
        for table, expected in required.items():
            data = payload(await self.call("+field-list", table, "--limit", "200"))
            fields = data.get("fields", data.get("items", []))
            actual = {field.get("name"): field.get("type") for field in fields}
            invalid = [name for name, kind in expected.items() if actual.get(name) != kind]
            if invalid:
                raise ValueError("多维表格字段缺失或类型不符：" + ", ".join(invalid))
        self.checked = True

    async def find(self, table, key, value):
        result = await self.call("+record-list", table, "--filter-json", json.dumps({"logic": "and", "conditions": [[key, "==", value]]}, ensure_ascii=False),
                                 "--field-id", key, "--limit", "2")
        found = records(result)
        if len(found) > 1 or payload(result).get("has_more"):
            raise ValueError("台账存在重复业务编号，需要人工核对：" + value)
        return record_id(found[0]) if found else None

    async def write(self, table, fields, existing=None):
        args = ["--json", json.dumps(fields, ensure_ascii=False)]
        if existing:
            args += ["--record-id", existing]
        data = payload(await self.call("+record-upsert", table, *args))
        if data.get("ignored_fields"):
            raise ValueError("飞书忽略了待写字段：" + str(data["ignored_fields"]))
        return returned_record_id(data)

    async def sync_issue(self, item):
        table = self.base["issues_table_id"]
        rid = item["record_id"]
        if not rid:
            rid = await self.find(table, "问题编号", item["issue_id"])
            if not rid:
                # Even if the network was down while a solution was proposed,
                # the first cloud record is created open. Later revisions follow.
                rid = await self.write(table, {
                    "问题编号": item["issue_id"], "问题标题": item["title"], "状态": "open",
                    "父问题编号": item["parent_id"] or "", "上报类型": "基于旧问题上报" if item["parent_id"] else "新问题",
                    "上报人": [{"id": item["reporter_id"]}], "上报时间": date(item["created_at"]),
                    "现象描述": item["description"], "最近活动时间": date(item["updated_at"]),
                    "完整记录": item["doc_url"] or "",
                })
                # v3 sometimes responds to a successful create with only the
                # changed fields. Re-read by the immutable business key before
                # recording local state, so a retry cannot create a duplicate.
                rid = rid or await self.find(table, "问题编号", item["issue_id"])
                if not rid:
                    raise ValueError("飞书创建问题记录后无法按问题编号找回：" + item["issue_id"])
            with self.store.db:
                self.store.db.execute("UPDATE issues SET record_id=? WHERE issue_id=?", (rid, item["issue_id"]))
        remote_rows = records(await self.call("+record-get", table, "--record-id", rid,
                                              "--field-id", "状态", "--field-id", "解决方案", "--field-id", "验证步骤"))
        if len(remote_rows) != 1:
            raise ValueError("问题台账记录缺失，停止自动重建：" + item["issue_id"])
        remote = remote_rows[0].get("fields", {})
        remote_status = cell_text(remote.get("状态"))
        solution, verification = cell_text(remote.get("解决方案")), cell_text(remote.get("验证步骤"))
        remote_signature = hashlib.sha256(json.dumps([remote_status, solution, verification]).encode()).hexdigest()
        item = self.store.issue(item["issue_id"])
        # Developer submission in the sheet is accepted only with a concrete
        # plan. A remote '已解决' cannot bypass the reporter's local confirmation.
        if (item["status"] != "已解决" and remote_status == "待验证" and solution.strip() and verification.strip()
                and remote_signature != item["remote_proposal"]):
            fingerprint = hashlib.sha256((rid + solution + verification).encode()).hexdigest()
            seen = self.store.db.execute("SELECT 1 FROM issue_entries WHERE entry_id=?", ("base:" + fingerprint + ":pending",)).fetchone()
            if not seen:
                self.store.propose(item, solution, verification, "开发人员（多维表格）", "base:" + fingerprint)
                item = self.store.issue(item["issue_id"])
        if item["synced_revision"] == item["revision"] and remote_status == item["status"]:
            return
        fields = {"问题标题": item["title"], "状态": item["status"], "现象描述": item["description"],
                  "父问题编号": item["parent_id"] or "", "上报类型": "基于旧问题上报" if item["parent_id"] else "新问题",
                  "最近活动时间": date(item["updated_at"]), "Codex会话": item["thread_id"] or "",
                  "完整记录": item["doc_url"] or ""}
        latest = self.store.db.execute("SELECT content FROM issue_entries WHERE issue_id=? AND kind='AI分析' ORDER BY created_at DESC,rowid DESC LIMIT 1", (item["issue_id"],)).fetchone()
        if latest:
            fields["最新AI分析"] = latest[0][:12000]
        original = self.store.db.execute("SELECT reply_target,message_id FROM inbox WHERE issue_id=? ORDER BY seq LIMIT 1", (item["issue_id"],)).fetchone()
        if original:
            fields["上报原消息"] = original[0] or original[1]
        if item["solution"]:
            fields.update({"解决方案": item["solution"][:12000], "验证步骤": item["verification"][:12000]})
        if item["confirmed_by"]:
            fields.update({"现场确认人": [{"id": item["confirmed_by"]}], "现场确认时间": date(item["confirmed_at"])})
        else:
            fields.update({"现场确认人": None, "现场确认时间": None})
        await self.write(table, fields, rid)
        written_signature = hashlib.sha256(json.dumps([fields["状态"], fields.get("解决方案", solution), fields.get("验证步骤", verification)]).encode()).hexdigest()
        with self.store.db:
            self.store.db.execute("UPDATE issues SET synced_revision=?,remote_proposal=?,sync_error=NULL,retry_at=0 WHERE issue_id=?",
                                  (item["revision"], written_signature, item["issue_id"]))

    async def sync_entry(self, entry):
        item = self.store.issue(entry["issue_id"])
        if not item["record_id"]:
            return
        table = self.base["history_table_id"]
        # Split long histories into immutable records, retaining every character.
        content = entry["content"]
        parts = [content[i:i + 10000] for i in range(0, len(content), 10000)] or [""]
        ids = []
        for i, part in enumerate(parts):
            key = entry["entry_id"] + ":" + str(i)
            rid = await self.find(table, "记录编号", key)
            if not rid:
                rid = await self.write(table, {"记录编号": key, "问题编号": item["issue_id"],
                    "所属问题": [{"id": item["record_id"]}], "类型": entry["kind"], "参与者": entry["actor"],
                    "时间": date(entry["created_at"]), "内容": part})
                rid = rid or await self.find(table, "记录编号", key)
                if not rid:
                    raise ValueError("飞书创建处理记录后无法按记录编号找回：" + key)
            ids.append(rid)
        with self.store.db:
            self.store.db.execute("UPDATE issue_entries SET record_id=?,sync_error=NULL,retry_at=0 WHERE entry_id=?", (json.dumps(ids), entry["entry_id"]))

    async def once(self):
        if not self.checked:
            await self.check()
        for item in self.store.db.execute("SELECT * FROM issues WHERE retry_at<=? ORDER BY created_at", (time.time(),)).fetchall():
            if item["synced_revision"] == item["revision"] and time.time() - self.last_poll.get(item["issue_id"], 0) < 30:
                continue
            try:
                await self.sync_issue(item)
                self.last_poll[item["issue_id"]] = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                with self.store.db:
                    self.store.db.execute("UPDATE issues SET sync_error=?,retry_at=? WHERE issue_id=?", (str(error)[:1500], time.time() + 30, item["issue_id"]))
        for entry in self.store.db.execute("SELECT * FROM issue_entries WHERE record_id IS NULL AND retry_at<=? ORDER BY created_at LIMIT 30", (time.time(),)).fetchall():
            try:
                await self.sync_entry(entry)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                with self.store.db:
                    self.store.db.execute("UPDATE issue_entries SET sync_error=?,retry_at=? WHERE entry_id=?", (str(error)[:1500], time.time() + 30, entry["entry_id"]))
