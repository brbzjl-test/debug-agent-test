"""Gateway application service."""

from typing import Any, Dict, Mapping, Optional

from .adapters import BaseAdapter, FeishuAdapter
from .auth import CallbackVerifier
from .errors import ConflictError, ValidationError
from .store import GatewayStore


class GatewayService:
    def __init__(
        self,
        store: GatewayStore,
        callback_verifier: CallbackVerifier,
        feishu_adapter: Optional[FeishuAdapter] = None,
        base_adapter: Optional[BaseAdapter] = None,
    ) -> None:
        self.store = store
        self.callback_verifier = callback_verifier
        self.feishu_adapter = feishu_adapter
        self.base_adapter = base_adapter

    def authenticate(self, authorization: str) -> str:
        prefix = "Bearer "
        if not authorization.startswith(prefix):
            from .errors import AuthenticationError

            raise AuthenticationError("Bearer token is required")
        return self.store.authenticate_device(authorization[len(prefix) :])

    def handoff(
        self,
        authorization: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        device_id = self.authenticate(authorization)
        return self.store.create_handoff(device_id, idempotency_key, payload)

    def sync(
        self, authorization: str, issue_id: str, after_seq: int
    ) -> Dict[str, Any]:
        device_id = self.authenticate(authorization)
        return self.store.sync_issue(device_id, issue_id, after_seq)

    def confirm_solution(
        self,
        authorization: str,
        idempotency_key: str,
        issue_id: str,
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        device_id = self.authenticate(authorization)
        version = payload.get("solution_version")
        reporter_id = payload.get("reporter_id")
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValidationError("solution_version must be an integer")
        if not isinstance(reporter_id, str):
            raise ValidationError("reporter_id must be a string")
        result = self.store.confirm_solution(
            device_id, issue_id, version, reporter_id, idempotency_key
        )
        self._update_feishu_card(issue_id, "closed")
        return result

    def report_verification_failure(
        self,
        authorization: str,
        idempotency_key: str,
        issue_id: str,
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        device_id = self.authenticate(authorization)
        version = payload.get("solution_version")
        reporter_id = payload.get("reporter_id")
        observation = payload.get("observation")
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValidationError("solution_version must be an integer")
        if not isinstance(reporter_id, str):
            raise ValidationError("reporter_id must be a string")
        if not isinstance(observation, str):
            raise ValidationError("observation must be a string")
        result = self.store.report_verification_failure(
            device_id,
            issue_id,
            version,
            reporter_id,
            observation,
            idempotency_key,
        )
        self._update_feishu_card(issue_id, "awaiting_engineer", observation)
        return result

    def card_callback(self, headers: Mapping[str, str], body: bytes) -> Dict[str, Any]:
        verified = self.callback_verifier.verify(headers, body)
        import json

        payload = json.loads(body.decode("utf-8"))
        if payload.get("action") != "submit_solution":
            raise ValidationError("unsupported card action")
        value = payload.get("value") or {}
        if not isinstance(value, dict):
            raise ValidationError("card value must be an object")
        requested_version = value.get("solution_version")
        if requested_version is not None:
            if isinstance(requested_version, bool) or not isinstance(requested_version, int):
                raise ValidationError("solution_version must be an integer")
        verification_method = value.get("verification_method")
        if verification_method is not None and not isinstance(verification_method, str):
            raise ValidationError("verification_method must be a string or null")
        actual_solution = value.get("actual_solution", "")
        if not isinstance(actual_solution, str):
            raise ValidationError("actual_solution must be a string")
        issue_id = value.get("issue_id", "")
        if not isinstance(issue_id, str):
            raise ValidationError("issue_id must be a string")
        result = self.store.submit_solution(
            callback_id=verified.event_id,
            engineer_id=verified.principal_id,
            issue_id=issue_id.strip(),
            actual_solution=actual_solution,
            verification_method=verification_method,
            requested_version=requested_version,
        )
        self._update_feishu_card(issue_id.strip(), "waiting_verification")
        return result

    def card_action_event(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        """Consume normalized lark-cli card.action.trigger output."""
        event_id = str(payload.get("event_id", "")).strip()
        operator_id = str(payload.get("operator_id", "")).strip()
        message_id = str(payload.get("message_id", "")).strip()
        if not event_id or not operator_id or not message_id:
            raise ValidationError("card event is missing event_id, operator_id or message_id")
        if payload.get("action_tag") != "button":
            raise ValidationError("only solution form submission is supported")
        raw_form = payload.get("form_value", "")
        if isinstance(raw_form, str):
            import json

            try:
                form = json.loads(raw_form)
            except json.JSONDecodeError as exc:
                raise ValidationError("form_value must be valid JSON") from exc
        elif isinstance(raw_form, dict):
            form = raw_form
        else:
            raise ValidationError("form_value must be an object")
        if not isinstance(form, dict):
            raise ValidationError("form_value must be an object")
        issue_id = self.store.issue_id_for_message(message_id)
        result = self.store.submit_solution(
            callback_id=event_id,
            engineer_id=operator_id,
            issue_id=issue_id,
            actual_solution=str(form.get("actual_solution", "")),
            verification_method=None,
            requested_version=None,
        )
        self._update_feishu_card(issue_id, "waiting_verification")
        return result

    def _update_feishu_card(
        self, issue_id: str, state: str, observation: Optional[str] = None
    ) -> None:
        if self.feishu_adapter is None or not hasattr(self.feishu_adapter, "update_issue_card"):
            return
        try:
            context = self.store.issue_card_context(issue_id)
        except ConflictError:
            return
        self.feishu_adapter.update_issue_card(
            context["message_id"],
            issue_id,
            state,
            context.get("latest_solution"),
            observation,
        )

    def event_callback(self, headers: Mapping[str, str], body: bytes) -> Dict[str, Any]:
        verified = self.callback_verifier.verify(headers, body)
        import json

        payload = json.loads(body.decode("utf-8"))
        value = payload.get("value") or {}
        if not isinstance(value, dict):
            raise ValidationError("event value must be an object")
        kind = str(payload.get("kind", ""))
        issue_id = value.get("issue_id", "")
        if not isinstance(issue_id, str):
            raise ValidationError("issue_id must be a string")
        return self.store.record_feishu_event(
            callback_id=verified.event_id,
            engineer_id=verified.principal_id,
            issue_id=issue_id.strip(),
            kind=kind,
            payload=value,
        )

    def dispatch_one_handoff(self) -> bool:
        if self.feishu_adapter is None:
            return False
        item = self.store.pending_delivery()
        if item is None:
            return False
        try:
            payload = dict(item["payload"])
            payload["_idempotency_key"] = item["idempotency_key"]
            root_issue_id = str(payload.get("root_issue_id") or payload["issue_id"])
            topic_root = self.store.topic_root_for(root_issue_id)
            if topic_root:
                payload["_topic_root_message_id"] = topic_root
            binding = self.feishu_adapter.send_handoff(payload)
            self.store.complete_delivery(item["outbox_id"], binding)
        except Exception as exc:
            self.store.fail_delivery(item["outbox_id"], str(exc))
            raise
        return True

    def dispatch_one_base_event(self) -> bool:
        if self.base_adapter is None:
            return False
        item = self.store.pending_base_event()
        if item is None:
            return False
        try:
            self.base_adapter.apply_event(item["payload"])
            self.store.complete_base_event(item["outbox_id"])
        except Exception as exc:
            self.store.fail_base_event(item["outbox_id"], str(exc))
            raise
        return True
