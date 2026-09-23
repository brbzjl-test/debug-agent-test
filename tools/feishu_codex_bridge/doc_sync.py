"""Create one Base-owned Feishu document per issue and append its full timeline."""
import html
import json
import re
import time
from urllib.parse import urlsplit


def payload(result):
    if result.get("ok") is False or result.get("code", 0) not in (0, None):
        raise RuntimeError("飞书返回错误：" + json.dumps(result, ensure_ascii=False)[:1000])
    return result["data"] if isinstance(result.get("data"), dict) else result


def xml_text(value):
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value))
    return html.escape(value, quote=False).replace("\n", "<br/>")


def issue_marker(issue_id):
    return "[问题同步标识:" + issue_id + "]"


def entry_marker(entry_id):
    return "[记录同步标识:" + entry_id + "]"


def header_xml(item, base_url):
    parent = item["parent_id"] or "无"
    description = item["description"] or "等待现场补充问题现象和日志。"
    created = time.strftime("%Y-%m-%d %H:%M", time.localtime(item["created_at"]))
    return (
        '<callout emoji="📌" background-color="light-blue" border-color="blue"><p>'
        "<b>问题编号：</b>{}<br/><b>初始状态：</b>open<br/><b>上报时间：</b>{}<br/>"
        "<b>上报人：</b><cite type=\"user\" user-id=\"{}\"></cite><br/><b>父问题：</b>{}"
        "</p></callout><h2>问题概况</h2><p>{}</p>"
        '<p><a href="{}">打开问题总台账</a></p><h2>完整对话</h2>'
        '<p><span text-color="gray">按时间顺序自动追加现场消息、Codex 回复和状态变化。</span></p>'
        '<p><span text-color="gray">{}</span></p><hr/>'
    ).format(
        xml_text(item["issue_id"]), xml_text(created), html.escape(item["reporter_id"], quote=True),
        xml_text(parent), xml_text(description), html.escape(base_url, quote=True), xml_text(issue_marker(item["issue_id"])),
    )


def entry_xml(entry):
    created = time.strftime("%m-%d %H:%M", time.localtime(entry["created_at"]))
    kind = xml_text(entry["kind"])
    if entry["actor"].startswith("ou_"):
        actor = '<cite type="user" user-id="{}"></cite>'.format(html.escape(entry["actor"], quote=True))
    elif entry["actor"] == "Codex":
        actor = '<span text-color="blue"><b>Codex</b></span>'
    else:
        actor = xml_text(entry["actor"])
    return (
        "<p><b>{} · {}</b> · {}</p><blockquote><p>{}</p></blockquote>"
        '<p><span text-color="gray">{}</span></p><hr/>'
    ).format(xml_text(created), kind, actor, xml_text(entry["content"]), xml_text(entry_marker(entry["entry_id"])))


class IssueDocSync:
    def __init__(self, config, store, runner):
        self.config, self.store, self.runner = config, store, runner
        self.base = config.get("base") or {}
        self.settings = config.get("issue_docs") or {}
        self.folder_id = self.settings.get("folder_id")

    async def call(self, *args):
        command = [self.config["lark_cli"], "--profile", self.config["lark_profile"], *args, "--format", "json"]
        return payload(json.loads(await self.runner(command)))

    async def blocks(self):
        return (await self.call("base", "+base-block-list", "--as", "bot", "--base-token", self.base["base_token"])).get("blocks", [])

    async def ensure_folder(self):
        if self.folder_id:
            return self.folder_id
        name = self.settings.get("folder_name", "问题详情")
        matches = [block for block in await self.blocks() if block.get("type") == "folder" and block.get("name") == name]
        if len(matches) > 1:
            raise ValueError("Base 中存在多个同名问题详情目录：" + name)
        if matches:
            self.folder_id = matches[0]["id"]
            return self.folder_id
        data = await self.call("base", "+base-block-create", "--as", "bot", "--base-token", self.base["base_token"],
                               "--type", "folder", "--name", name)
        self.folder_id = data["block"]["id"]
        return self.folder_id

    def document_url(self, token):
        host = urlsplit(self.base["url"]).netloc
        return "https://{}/docx/{}".format(host, token)

    async def ensure_document(self, item):
        if item["doc_token"]:
            return self.store.issue(item["issue_id"])
        folder = await self.ensure_folder()
        name = item["issue_id"] + "｜现场问题记录"
        matches = [block for block in await self.blocks()
                   if block.get("type") == "docx" and block.get("name") == name and block.get("parent_id") == folder]
        if len(matches) > 1:
            raise ValueError("问题存在多个独立文档，需要人工核对：" + item["issue_id"])
        if matches:
            block = matches[0]
        else:
            data = await self.call("base", "+base-block-create", "--as", "bot", "--base-token", self.base["base_token"],
                                   "--type", "docx", "--name", name, "--parent-id", folder)
            block = data["block"]
        token = block["docx_token"]
        with self.store.db:
            self.store.db.execute(
                "UPDATE issues SET doc_token=?,doc_url=?,doc_block_id=?,doc_sync_error=NULL,doc_retry_at=0,"
                "revision=revision+1,updated_at=? WHERE issue_id=?",
                (token, self.document_url(token), block.get("id"), time.time(), item["issue_id"]),
            )
        return self.store.issue(item["issue_id"])

    async def fetch(self, token):
        data = await self.call("docs", "+fetch", "--as", "bot", "--doc", token, "--detail", "simple")
        return data["document"]

    async def append(self, token, content, revision):
        return (await self.call("docs", "+update", "--as", "bot", "--doc", token, "--command", "append",
                                "--revision-id", str(revision), "--content", content))["document"]

    def notify_ready(self, item):
        if item["doc_notified"]:
            return
        message = self.store.db.execute(
            "SELECT * FROM inbox WHERE issue_id=? AND sender_id=? ORDER BY seq DESC LIMIT 1",
            (item["issue_id"], item["reporter_id"]),
        ).fetchone()
        if not message:
            return
        with self.store.db:
            self.store._reply(message["message_id"], "doc-ready", "[{}] 独立完整记录：{}".format(item["issue_id"], item["doc_url"]))
            self.store.db.execute("UPDATE issues SET doc_notified=1 WHERE issue_id=?", (item["issue_id"],))

    async def sync_document(self, item):
        pending = self.store.db.execute(
            "SELECT * FROM issue_entries WHERE issue_id=? AND doc_synced_at IS NULL AND doc_retry_at<=? ORDER BY created_at,rowid",
            (item["issue_id"], time.time()),
        ).fetchall()
        if item["doc_initialized"] and not pending:
            self.notify_ready(item)
            return
        document = await self.fetch(item["doc_token"])
        content, revision = document.get("content", ""), document["revision_id"]
        if issue_marker(item["issue_id"]) not in content:
            document = await self.append(item["doc_token"], header_xml(item, self.base["url"]), revision)
            revision = document["revision_id"]
            content += issue_marker(item["issue_id"])
        with self.store.db:
            self.store.db.execute("UPDATE issues SET doc_initialized=1,doc_sync_error=NULL,doc_retry_at=0 WHERE issue_id=?", (item["issue_id"],))
            for entry in pending:
                if entry_marker(entry["entry_id"]) in content:
                    self.store.db.execute(
                        "UPDATE issue_entries SET doc_synced_at=?,doc_sync_error=NULL,doc_retry_at=0 WHERE entry_id=?",
                        (time.time(), entry["entry_id"]),
                    )
        missing = [entry for entry in pending if entry_marker(entry["entry_id"]) not in content]
        if missing:
            await self.append(item["doc_token"], "".join(entry_xml(entry) for entry in missing), revision)
            with self.store.db:
                now = time.time()
                self.store.db.executemany(
                    "UPDATE issue_entries SET doc_synced_at=?,doc_sync_error=NULL,doc_retry_at=0 WHERE entry_id=?",
                    [(now, entry["entry_id"]) for entry in missing],
                )
        self.notify_ready(self.store.issue(item["issue_id"]))

    async def once(self):
        if not self.settings or not self.base.get("base_token") or not self.base.get("url"):
            return
        for item in self.store.db.execute("SELECT * FROM issues WHERE doc_retry_at<=? ORDER BY created_at", (time.time(),)).fetchall():
            try:
                item = await self.ensure_document(item)
                await self.sync_document(item)
            except Exception as error:
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE issues SET doc_sync_error=?,doc_retry_at=? WHERE issue_id=?",
                        (str(error)[:1500], time.time() + 30, item["issue_id"]),
                    )
