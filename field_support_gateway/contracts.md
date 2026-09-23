# Gateway contracts

## Device handoff

```json
{
  "issue_id": "ISS-20260916-00000001",
  "root_issue_id": "ISS-20260916-00000001",
  "reporter_id": "site-user",
  "summary": "Device is not detected",
  "evidence": [{"snapshot_id": "SNP-1", "sha256": "..."}]
}
```

The device sends a stable `Idempotency-Key`. Reusing that key with identical
content returns the original result; reusing it with different content returns
HTTP 409.

`root_issue_id` is also the Feishu topic key. The first human handoff in an
issue family creates the topic root. Every later root or sub-issue card and
evidence attachment is sent as a thread reply to that root. Each issue keeps
its own `message_id` for in-place status updates, while every member of the
family shares one `topic_id`.

## Normalized card callback

The HTTP callback verifier is replaceable. The built-in parser accepts this
normalized envelope after signature verification:

```json
{
  "event_id": "feishu-event-id",
  "action": "submit_solution",
  "operator": {"open_id": "ou_engineer"},
  "value": {
    "issue_id": "ISS-20260916-00000001",
    "solution_version": 1,
    "actual_solution": "Reconnected the USB cable and restarted the process",
    "verification_method": "Optional field verification notes"
  }
}
```

`actual_solution` must be non-empty. `verification_method` is optional. The
engineer identity comes from the verified callback envelope and must be in the
server-side engineer allowlist.

After a Solution is submitted, the Gateway replaces the form with a read-only
waiting-for-verification card. The field device can reject that version with:

```json
{
  "solution_version": 1,
  "reporter_id": "site-user",
  "observation": "The device is still missing after reconnecting the cable"
}
```

Send this body to `POST /v1/issues/{issue_id}/verification-failure` with device
authentication and an `Idempotency-Key`. The same issue returns to `open`, the
failure is recorded as an event, and the Feishu card exposes a new Solution
form. A later Solution uses the next version number.
