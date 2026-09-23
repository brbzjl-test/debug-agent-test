# Field Support App

Standalone local application for reporting, collecting and analyzing field issues.

The application treats every configured business repository as read-only. It does not require or modify business logging, ROS nodes, drivers, startup scripts, or deployment code.

## Ubuntu package

Install the generated `.deb` and connect an existing local business repository:

```bash
sudo apt install ./field-support-agent_0.1.0_all.deb
sudo field-support-setup --repo "$HOME/business/debug-agent-test"
```

The setup command discovers the repository's `origin`, writes an absolute-path
configuration, and registers the Core and desktop startup entries. Python wheels
are downloaded during setup; the Codex CLI and its login remain separate. On
package upgrade, run `sudo field-support-setup --reuse`. See
`DEPLOY_UBUNTU.md` for service, browser, and SSH access instructions.

## Initial configuration

`config.example.yaml` seeds the first business repository. After startup, open
the gear button in the local UI to manage business repositories, optional ROS
topology, business log paths, Codex, Feishu, and Base from one settings page.

Private settings are stored in the selected state directory as `settings.json`
with mode `0600`. Feishu App Secret is never returned by the local API. The app
uses the official Feishu Python SDK and does not require `lark-cli`. It exchanges
App ID and App Secret for temporary platform tokens automatically.

Configure one absolute path per line under `业务日志目录`; snapshot collection
reads bounded log files from these directories. Set `现场名称` and `设备名称`
in the Feishu section to create or reuse the matching support group after saving.
The bot that creates the group is already a member.

## Run locally

```bash
PYTHONPATH=src python3 -m field_support_agent.app serve --config config.example.yaml --browser
```

The Feishu WebSocket connection is opened only after the reporter selects human
support. Closing the application stops it. Codex model and reasoning settings
can change, while the read-only sandbox and disabled write-capable tools remain
mandatory.

## Streaming replies

AI chat uses the configured Codex binary's `app-server` text-delta interface.
It starts only for analysis and stops when that turn finishes. During an active
analysis the chat refreshes the partial reply every 200 ms; the composer stays
locked until completion. Only the complete reply is written to the conversation
database. Interrupted replies are replaced by a failure notice, and reopening an
active issue resumes displaying the current partial reply.

The assistant message bubble shows a short activity label instead of typing dots, derived from observed Codex
events (for example, reading logs, inspecting code, or generating a reply), plus
elapsed time and seconds since the last activity. Raw reasoning, commands,
paths, and tool output are not displayed as progress. After 20 seconds without
an event it shows a waiting notice; silence alone is not diagnosed as a network
failure. Local API connection failures are shown separately. Polling never
resets the activity clock, and progress exists only while a task is active.

If an existing Codex session reports an active writer, the chat explains that
another window or process occupies the session and this analysis did not start.
The app does not retry, create or switch sessions, or release the other writer.

The server must confirm `readOnly` and `never` before a turn starts. If user or
project `.rules` files are present, the runner retains the original
`codex exec --ignore-rules` path and displays the completed response instead;
streaming must not weaken the existing isolation policy.

Validate with `python -m unittest discover -s tests -v` and
`node --test tests/test_ui_streaming.js`.

## Deleting history

Management-mode deletion removes the selected issues, their local database
records, snapshots, analysis files, and associated Codex conversations. Codex
sessions are deleted by their mapped UUID through `thread/delete`; unrelated
sessions in the same Codex home are never selected.

Subissues share their root's Codex conversation. Deleting a subissue therefore
also deletes that shared Codex conversation and clears its mapping. The remaining
local issue history stays available; the next analysis builds a new conversation
from those surviving records. Deleting a root also deletes its subissues.

Deletion is rejected while a related analysis is running. If Codex cleanup fails,
local issues and files are retained for retry. Successfully cleaned mappings are
removed immediately, so a partially completed batch can safely be retried.
Old Codex conversations whose issue mappings were deleted by earlier versions
are not automatically discovered or removed.

## Development status

P0 is implemented. Contracts in `contracts/` remain the integration boundary between the local application and Gateway.
