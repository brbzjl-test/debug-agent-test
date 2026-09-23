from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Optional


class GatewayError(RuntimeError):
    pass


class GatewayClient:
    """Small device-side client; it is only used after explicit human handoff."""

    def __init__(self, base_url: str, device_token: str, *, timeout_seconds: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.device_token = device_token
        self.timeout_seconds = timeout_seconds
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("gateway base URL must use http or https")
        if not device_token:
            raise ValueError("device token is required")

    def handoff(self, payload: Mapping[str, Any], idempotency_key: str) -> Mapping[str, Any]:
        return self._request(
            "POST",
            "/v1/handoffs",
            payload,
            {"Idempotency-Key": idempotency_key},
        )

    def sync(self, issue_id: str, after_seq: int = 0) -> Mapping[str, Any]:
        path = "/v1/issues/{}/sync?after_seq={}".format(
            urllib.parse.quote(issue_id, safe=""), max(0, int(after_seq))
        )
        return self._request("GET", path)

    def confirm(self, issue_id: str, solution_version: int, reporter_id: str) -> Mapping[str, Any]:
        path = "/v1/issues/{}/confirm".format(urllib.parse.quote(issue_id, safe=""))
        return self._request(
            "POST",
            path,
            {"solution_version": solution_version, "reporter_id": reporter_id},
            {"Idempotency-Key": "confirm:{}:{}".format(issue_id, solution_version)},
        )

    def verification_failed(
        self,
        issue_id: str,
        solution_version: int,
        reporter_id: str,
        observation: str,
    ) -> Mapping[str, Any]:
        path = "/v1/issues/{}/verification-failure".format(urllib.parse.quote(issue_id, safe=""))
        return self._request(
            "POST",
            path,
            {
                "solution_version": solution_version,
                "reporter_id": reporter_id,
                "observation": observation,
            },
            {"Idempotency-Key": "verification-failure:{}:{}".format(issue_id, solution_version)},
        )

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Mapping[str, Any]] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> Mapping[str, Any]:
        headers = {"Authorization": "Bearer " + self.device_token, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                value = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GatewayError("gateway request failed: {}".format(exc)) from exc
        if not isinstance(value, dict):
            raise GatewayError("gateway returned a non-object response")
        return value
