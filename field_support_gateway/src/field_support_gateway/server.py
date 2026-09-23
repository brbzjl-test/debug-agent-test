"""Standard-library HTTP server for the field support gateway."""

import argparse
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from .adapters import LarkCliBaseAdapter, LarkCliFeishuAdapter
from .auth import HmacCallbackVerifier
from .card_events import LarkCardEventConsumer
from .errors import GatewayError, ValidationError
from .service import GatewayService
from .store import GatewayStore
from .worker import OutboxWorker


MAX_BODY_BYTES = 2 * 1024 * 1024
SYNC_PATH = re.compile(r"^/v1/issues/([^/]+)/sync$")
CONFIRM_PATH = re.compile(r"^/v1/issues/([^/]+)/confirm$")
VERIFICATION_FAILURE_PATH = re.compile(
    r"^/v1/issues/([^/]+)/verification-failure$"
)


class GatewayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: Tuple[str, int],
        service: GatewayService,
        worker: Optional[OutboxWorker] = None,
    ) -> None:
        self.service = service
        self.worker = worker
        super().__init__(address, GatewayRequestHandler)

    def notify_outbox(self) -> None:
        if self.worker is not None:
            self.worker.notify()


class GatewayRequestHandler(BaseHTTPRequestHandler):
    server: GatewayHTTPServer

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._write_json(200, {"status": "ok"})
            return
        match = SYNC_PATH.match(parsed.path)
        if match:
            try:
                values = parse_qs(parsed.query)
                raw_after = values.get("after_seq", ["0"])[0]
                after_seq = int(raw_after)
                if after_seq < 0:
                    raise ValueError
                result = self.server.service.sync(
                    self.headers.get("Authorization", ""),
                    unquote(match.group(1)),
                    after_seq,
                )
                self._write_json(200, result)
            except ValueError:
                self._write_error(ValidationError("after_seq must be a non-negative integer"))
            except GatewayError as exc:
                self._write_error(exc)
            return
        self._write_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        try:
            body = self._read_body()
            if self.path == "/v1/handoffs":
                payload = self._decode_object(body)
                result = self.server.service.handoff(
                    self.headers.get("Authorization", ""),
                    self.headers.get("Idempotency-Key", ""),
                    payload,
                )
                self._write_json(202, result)
                self.server.notify_outbox()
            elif CONFIRM_PATH.match(urlparse(self.path).path):
                match = CONFIRM_PATH.match(urlparse(self.path).path)
                assert match is not None
                payload = self._decode_object(body)
                result = self.server.service.confirm_solution(
                    self.headers.get("Authorization", ""),
                    self.headers.get("Idempotency-Key", ""),
                    unquote(match.group(1)),
                    payload,
                )
                self._write_json(200, result)
                self.server.notify_outbox()
            elif VERIFICATION_FAILURE_PATH.match(urlparse(self.path).path):
                match = VERIFICATION_FAILURE_PATH.match(urlparse(self.path).path)
                assert match is not None
                payload = self._decode_object(body)
                result = self.server.service.report_verification_failure(
                    self.headers.get("Authorization", ""),
                    self.headers.get("Idempotency-Key", ""),
                    unquote(match.group(1)),
                    payload,
                )
                self._write_json(200, result)
                self.server.notify_outbox()
            elif self.path == "/v1/feishu/card-actions":
                result = self.server.service.card_callback(dict(self.headers.items()), body)
                self._write_json(200, result)
                self.server.notify_outbox()
            elif self.path == "/v1/feishu/events":
                result = self.server.service.event_callback(dict(self.headers.items()), body)
                self._write_json(200, result)
                self.server.notify_outbox()
            else:
                self._write_json(404, {"error": "not_found"})
        except GatewayError as exc:
            self._write_error(exc)

    def _read_body(self) -> bytes:
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValidationError("valid Content-Length is required") from exc
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValidationError("request body is too large")
        return self.rfile.read(length)

    @staticmethod
    def _decode_object(body: bytes) -> Mapping[str, Any]:
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("body must be valid JSON") from exc
        if not isinstance(value, dict):
            raise ValidationError("body must be a JSON object")
        return value

    def _write_error(self, error: GatewayError) -> None:
        self._write_json(
            error.status_code, {"error": error.code, "message": str(error)}
        )

    def _write_json(self, status: int, value: Mapping[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def build_service_from_env() -> GatewayService:
    db_path = os.environ.get("FIELD_SUPPORT_DB", "gateway.db")
    callback_secret = os.environ.get("FIELD_SUPPORT_CALLBACK_SECRET", "")
    if not callback_secret:
        raise RuntimeError("FIELD_SUPPORT_CALLBACK_SECRET is required")
    store = GatewayStore(db_path)
    devices = json.loads(os.environ.get("FIELD_SUPPORT_DEVICES", "{}"))
    engineers = json.loads(os.environ.get("FIELD_SUPPORT_ENGINEERS", "[]"))
    if not isinstance(devices, dict) or not devices:
        raise RuntimeError("FIELD_SUPPORT_DEVICES must be a non-empty JSON object")
    if not isinstance(engineers, list):
        raise RuntimeError("FIELD_SUPPORT_ENGINEERS must be a JSON array")
    for device_id, token in devices.items():
        store.register_device(str(device_id), str(token))
    for engineer_id in engineers:
        store.register_engineer(str(engineer_id))

    feishu_adapter = None
    profile = os.environ.get("FIELD_SUPPORT_LARK_PROFILE")
    support_chat_id = os.environ.get("FIELD_SUPPORT_SUPPORT_CHAT_ID")
    if profile and support_chat_id:
        feishu_adapter = LarkCliFeishuAdapter(
            profile=profile,
            support_chat_id=support_chat_id,
            binary=os.environ.get("FIELD_SUPPORT_LARK_CLI", "lark-cli"),
        )
    base_adapter = None
    base_token = os.environ.get("FIELD_SUPPORT_BASE_TOKEN")
    base_table_id = os.environ.get("FIELD_SUPPORT_BASE_TABLE_ID")
    if profile and base_token and base_table_id:
        base_adapter = LarkCliBaseAdapter(
            profile=profile,
            app_token=base_token,
            table_id=base_table_id,
            binary=os.environ.get("FIELD_SUPPORT_LARK_CLI", "lark-cli"),
        )
    return GatewayService(
        store=store,
        callback_verifier=HmacCallbackVerifier(callback_secret),
        feishu_adapter=feishu_adapter,
        base_adapter=base_adapter,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    service = build_service_from_env()
    worker = OutboxWorker(service)
    server = GatewayHTTPServer((args.host, args.port), service, worker)
    profile = os.environ.get("FIELD_SUPPORT_LARK_PROFILE")
    card_consumer = None
    if profile and os.environ.get("FIELD_SUPPORT_LARK_CARD_EVENTS", "1") != "0":
        card_consumer = LarkCardEventConsumer(
            service,
            profile,
            binary=os.environ.get("FIELD_SUPPORT_LARK_CLI", "lark-cli"),
        )
    worker.start()
    if card_consumer is not None:
        card_consumer.start()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if card_consumer is not None:
            card_consumer.stop()
        worker.stop()


if __name__ == "__main__":
    main()
