# Field Support Gateway

`field_support_gateway` is the server-side authority for human handoffs,
Feishu callback events, Solution versions, and Base projection events. It uses
only the Python standard library at runtime and supports Python 3.9.

The service does not store bot credentials on field devices. Device API tokens,
the callback secret, and the optional `lark-cli` profile are supplied to this
server through environment variables.

## Run

```bash
cd field_support_gateway
PYTHONPATH=src \
FIELD_SUPPORT_DB=/var/lib/field-support-gateway/gateway.db \
FIELD_SUPPORT_DEVICES='{"station-1":"replace-with-random-token"}' \
FIELD_SUPPORT_ENGINEERS='["ou_engineer"]' \
FIELD_SUPPORT_CALLBACK_SECRET='replace-with-callback-secret' \
python3 -m field_support_gateway.server
```

Optional Feishu delivery variables:

```text
FIELD_SUPPORT_LARK_PROFILE
FIELD_SUPPORT_SUPPORT_CHAT_ID
FIELD_SUPPORT_LARK_CLI
FIELD_SUPPORT_BASE_TOKEN
FIELD_SUPPORT_BASE_TABLE_ID
FIELD_SUPPORT_LARK_CARD_EVENTS=1
```

When the matching adapter variables are configured, a local Outbox worker drains
pending work at startup and when requests create new events. Without them, work
remains durably queued. The worker never scans Feishu or Base periodically.

## API

- `POST /v1/handoffs` requires `Authorization: Bearer ...` and
  `Idempotency-Key`.
- `GET /v1/issues/{issue_id}/sync?after_seq=N` requires device authentication
  and always returns materialized issue state plus any newer events.
- `POST /v1/issues/{issue_id}/confirm` closes the matching Solution version and
  projects `已解决` to Base.
- `POST /v1/issues/{issue_id}/verification-failure` reopens the same issue with
  the field observation and restores the engineer Solution form on its card.
- `POST /v1/feishu/events` and `POST /v1/feishu/card-actions` require callback
  signature headers.

With `FIELD_SUPPORT_LARK_PROFILE` configured, the Gateway consumes
`card.action.trigger` over Feishu's long connection and accepts Solution forms
only from IDs in `FIELD_SUPPORT_ENGINEERS`. A Feishu app permits one consumer
for this subscription, so old bridge consumers must be stopped before Gateway
startup.

Feishu threads are allocated per root issue. Sub-issues reuse the root issue's
thread and keep separate card message IDs inside it, so status changes update
the correct card without creating another topic.

The normalized card callback payload is documented in `contracts.md`.
