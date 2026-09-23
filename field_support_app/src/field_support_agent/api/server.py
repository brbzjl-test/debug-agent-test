import hmac
import json
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional, Set, Tuple
from urllib.parse import urlsplit

from field_support_agent.collectors import BusinessStatusCollector
from field_support_agent.domain import ConflictError, DomainError, ForbiddenError, NotFoundError, ValidationError
from field_support_agent.management import ManagementAccess, ManagementAccessError
from field_support_agent.service import CoreService
from field_support_agent.settings import SettingsError, SettingsStore
from field_support_agent.startup import StartupPreference


class LocalAPIServer:
    """Authenticated JSON API bound exclusively to the IPv4 loopback."""

    def __init__(
        self,
        service: CoreService,
        port: int = 0,
        session_token: Optional[str] = None,
        *,
        settings: Optional[SettingsStore] = None,
        management_access: Optional[ManagementAccess] = None,
        on_settings: Optional[Callable[[Dict[str, Any]], None]] = None,
        startup_preference: Optional[StartupPreference] = None,
    ):
        self.service = service
        self.settings = settings
        self.management_access = management_access
        self.on_settings = on_settings
        self.startup_preference = startup_preference
        self.session_token = session_token or secrets.token_urlsafe(32)
        if len(self.session_token) < 32:
            raise ValueError("session token must contain at least 32 characters")
        handler = self._handler_type()
        self._server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self._server.daemon_threads = True
        self._thread = None  # type: Optional[threading.Thread]

    @property
    def address(self) -> Tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("server is already running")
        self._thread = threading.Thread(target=self._server.serve_forever, name="field-support-api", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5)
            self._thread = None
        self._server.server_close()

    def _handler_type(self):
        service = self.service
        settings = self.settings
        management_access = self.management_access
        on_settings = self.on_settings
        startup_preference = self.startup_preference
        token = self.session_token

        class Handler(BaseHTTPRequestHandler):
            server_version = "FieldSupportCore/0.1"
            MAX_BODY = 1_048_576

            def log_message(self, format_string: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                try:
                    path = urlsplit(self.path).path
                    if path == "/health":
                        self._reply(HTTPStatus.OK, {"status": "ok"})
                        return
                    self._authenticate()
                    if path == "/v1/settings":
                        if settings is None:
                            raise NotFoundError("settings are unavailable")
                        self._reply(HTTPStatus.OK, {"settings": settings.public()})
                        return
                    if path == "/v1/startup":
                        self._reply(
                            HTTPStatus.OK,
                            startup_preference.status() if startup_preference else {"available": False, "enabled": False},
                        )
                        return
                    if path == "/v1/management/status":
                        if management_access is None:
                            raise NotFoundError("management access is unavailable")
                        self._reply(HTTPStatus.OK, {"configured": management_access.configured})
                        return
                    if path == "/v1/business-status":
                        if settings is None:
                            raise NotFoundError("settings are unavailable")
                        repositories = settings.runtime()["business"]["repositories"]
                        report = BusinessStatusCollector(repositories).collect()
                        self._reply(HTTPStatus.OK, {"business_status": report.to_dict()})
                        return
                    if path == "/v1/issues":
                        issues = []
                        for item in service.list_issues():
                            value = item.to_dict()
                            value["summary"] = service.issue_summary(item.issue_id)
                            issues.append(value)
                        self._reply(HTTPStatus.OK, {"issues": issues})
                        return
                    parts = self._parts(path)
                    if len(parts) == 3 and parts[:2] == ["v1", "issues"]:
                        self._reply(HTTPStatus.OK, {"issue": service.get_issue(parts[2]).to_dict()})
                        return
                    if len(parts) == 4 and parts[:2] == ["v1", "issues"] and parts[3] == "timeline":
                        timeline = service.timeline(parts[2])
                        self._reply(
                            HTTPStatus.OK,
                            {
                                "issue": timeline["issue"].to_dict(),
                                "messages": [item.to_dict() for item in timeline["messages"]],
                                "solutions": [item.to_dict() for item in timeline["solutions"]],
                                "events": [item.to_dict() for item in timeline["events"]],
                                "analysis": timeline.get("analysis"),
                            },
                        )
                        return
                    self._reply(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found", "message": "route not found"}})
                except Exception as exc:
                    self._error(exc)

            def do_POST(self) -> None:
                try:
                    self._authenticate()
                    path = urlsplit(self.path).path
                    body = self._json_body()
                    if path == "/v1/management/session":
                        if management_access is None:
                            raise NotFoundError("management access is unavailable")
                        self._fields(body, {"password"}, set())
                        management_token, created, expires_in = management_access.unlock(body["password"])
                        self._reply(
                            HTTPStatus.OK,
                            {
                                "management_token": management_token,
                                "password_created": created,
                                "expires_in": expires_in,
                            },
                        )
                        return
                    if path == "/v1/management/session/close":
                        if management_access is None:
                            raise NotFoundError("management access is unavailable")
                        self._fields(body, set(), set())
                        management_access.lock(self.headers.get("X-Management-Token", ""))
                        self._reply(HTTPStatus.OK, {"locked": True})
                        return
                    if path == "/v1/management/issues/delete":
                        if management_access is None:
                            raise NotFoundError("management access is unavailable")
                        management_access.require(self.headers.get("X-Management-Token", ""))
                        self._fields(body, {"issue_ids"}, set())
                        deleted = service.delete_issues(body["issue_ids"])
                        self._reply(HTTPStatus.OK, {"deleted_issue_ids": deleted})
                        return
                    if path == "/v1/settings/verify-feishu":
                        if settings is None:
                            raise NotFoundError("settings are unavailable")
                        self._fields(
                            body,
                            set(),
                            {"connection_mode", "app_id", "app_secret", "lark_cli_binary", "lark_profile"},
                        )
                        self._reply(HTTPStatus.OK, settings.verify_feishu(body))
                        return
                    if path == "/v1/issues":
                        self._fields(body, {"reporter_id"}, {"description"})
                        issue = service.create_issue(body["reporter_id"], body.get("description"))
                        self._reply(HTTPStatus.CREATED, {"issue": issue.to_dict()})
                        return
                    parts = self._parts(path)
                    if len(parts) != 4 or parts[:2] != ["v1", "issues"]:
                        self._reply(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found", "message": "route not found"}})
                        return
                    issue_id, action = parts[2], parts[3]
                    if action == "subissues":
                        self._fields(body, {"reporter_id"}, {"description"})
                        result = service.create_subissue(issue_id, body["reporter_id"], body.get("description"))
                        self._reply(HTTPStatus.CREATED, {"issue": result.to_dict()})
                    elif action == "messages":
                        self._fields(body, {"actor_id", "role", "content"}, {"channel"})
                        result = service.append_message(
                            issue_id, body["actor_id"], body["role"], body["content"], body.get("channel", "local")
                        )
                        self._reply(HTTPStatus.CREATED, {"message": result.to_dict()})
                    elif action == "handoff":
                        self._fields(body, {"actor_id"}, set())
                        result = service.request_handoff(issue_id, body["actor_id"])
                        self._reply(HTTPStatus.OK, {"issue": result.to_dict()})
                    elif action == "handoff-delivered":
                        self._fields(body, set(), {"actor_id"})
                        result = service.mark_handoff_delivered(issue_id, body.get("actor_id", "gateway"))
                        self._reply(HTTPStatus.OK, {"issue": result.to_dict()})
                    elif action == "solutions":
                        self._fields(body, {"submitted_by", "content"}, {"verification_method", "source"})
                        result = service.submit_solution(
                            issue_id,
                            body["submitted_by"],
                            body["content"],
                            body.get("verification_method"),
                            body.get("source", "local"),
                        )
                        self._reply(HTTPStatus.CREATED, {"solution": result.to_dict()})
                    elif action == "confirm":
                        self._fields(body, {"reporter_id", "expected_solution_version"}, set())
                        result = service.confirm_solution(
                            issue_id, body["reporter_id"], body["expected_solution_version"]
                        )
                        self._reply(HTTPStatus.OK, {"issue": result.to_dict()})
                    elif action == "confirm-ai-resolution":
                        self._fields(body, {"reporter_id"}, set())
                        result = service.confirm_ai_resolution(issue_id, body["reporter_id"])
                        self._reply(HTTPStatus.OK, {"issue": result.to_dict()})
                    elif action == "verification-failure":
                        self._fields(body, {"reporter_id", "observation"}, set())
                        result = service.report_verification_failure(
                            issue_id, body["reporter_id"], body["observation"]
                        )
                        self._reply(HTTPStatus.OK, {"issue": result.to_dict()})
                    else:
                        self._reply(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found", "message": "route not found"}})
                except Exception as exc:
                    self._error(exc)

            def do_PUT(self) -> None:
                try:
                    self._authenticate()
                    path = urlsplit(self.path).path
                    if path == "/v1/startup":
                        if startup_preference is None:
                            raise NotFoundError("自启动设置只在 Ubuntu 安装版可用")
                        body = self._json_body()
                        self._fields(body, {"enabled"}, set())
                        self._reply(HTTPStatus.OK, startup_preference.set_enabled(body["enabled"]))
                        return
                    if path != "/v1/settings":
                        self._reply(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found", "message": "route not found"}})
                        return
                    if settings is None:
                        raise NotFoundError("settings are unavailable")
                    body = self._json_body()
                    public = settings.update(body)
                    if on_settings is not None:
                        on_settings(settings.runtime())
                    self._reply(HTTPStatus.OK, {"settings": public})
                except Exception as exc:
                    self._error(exc)

            def _authenticate(self) -> None:
                provided = self.headers.get("Authorization", "")
                expected = "Bearer " + token
                if not hmac.compare_digest(provided, expected):
                    raise _Unauthorized("missing or invalid bearer token")

            def _json_body(self) -> Dict[str, Any]:
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/json":
                    raise ValidationError("Content-Type must be application/json")
                raw_length = self.headers.get("Content-Length")
                if raw_length is None:
                    raise ValidationError("Content-Length is required")
                try:
                    length = int(raw_length)
                except ValueError as exc:
                    raise ValidationError("invalid Content-Length") from exc
                if length < 0 or length > self.MAX_BODY:
                    raise ValidationError("request body is too large")
                try:
                    value = json.loads(self.rfile.read(length).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValidationError("request body must be valid UTF-8 JSON") from exc
                if not isinstance(value, dict):
                    raise ValidationError("request body must be a JSON object")
                return value

            @staticmethod
            def _fields(body: Dict[str, Any], required: Set[str], optional: Set[str]) -> None:
                missing = required - set(body)
                unknown = set(body) - required - optional
                if missing:
                    raise ValidationError("missing field: {}".format(sorted(missing)[0]))
                if unknown:
                    raise ValidationError("unknown field: {}".format(sorted(unknown)[0]))

            @staticmethod
            def _parts(path: str):
                return [part for part in path.split("/") if part]

            def _error(self, exc: Exception) -> None:
                if isinstance(exc, _Unauthorized):
                    status = HTTPStatus.UNAUTHORIZED
                    code = "unauthorized"
                elif isinstance(exc, (ValidationError, SettingsError)):
                    status = HTTPStatus.BAD_REQUEST
                    code = getattr(exc, "code", "invalid_settings")
                elif isinstance(exc, NotFoundError):
                    status = HTTPStatus.NOT_FOUND
                    code = exc.code
                elif isinstance(exc, (ForbiddenError, ManagementAccessError, PermissionError)):
                    status = HTTPStatus.FORBIDDEN
                    code = getattr(exc, "code", "forbidden")
                elif isinstance(exc, ConflictError):
                    status = HTTPStatus.CONFLICT
                    code = exc.code
                elif isinstance(exc, DomainError):
                    status = HTTPStatus.BAD_REQUEST
                    code = exc.code
                else:
                    status = HTTPStatus.INTERNAL_SERVER_ERROR
                    code = "internal_error"
                message = str(exc) if status != HTTPStatus.INTERNAL_SERVER_ERROR else "internal server error"
                self._reply(status, {"error": {"code": code, "message": message}})

            def _reply(self, status: HTTPStatus, body: Dict[str, Any]) -> None:
                encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self.send_response(int(status))
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(encoded)

        return Handler


class _Unauthorized(Exception):
    pass
