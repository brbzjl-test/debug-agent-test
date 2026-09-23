# P0 Domain Contract

## Status

Stored values are `open`, `pending_verification`, and `closed`. UI labels are `open`, `待验证`, and `已解决`.

- Creating a main issue or sub-issue produces `open`.
- Submitting a non-empty Solution changes `open` to `pending_verification`.
- Only the reporter may confirm the latest Solution version and change `pending_verification` to `closed`.
- Direct `open` to `closed` is forbidden.
- Reoccurrence creates a new sub-ID under the root and changes the root rollup to `open`; prior close events remain immutable.

## IDs

- Main issue: `ISS-YYYYMMDD-XXXXXXXX`.
- Sub-issue: `<root-id>-SNNN`.
- Selecting an existing sub-issue still allocates the next sub-ID under its root.

## Local events

Every event has:

```json
{
  "schema_version": 1,
  "event_id": "uuid",
  "issue_id": "ISS-...",
  "kind": "IssueCreated",
  "actor_id": "local-user",
  "channel": "local",
  "occurred_at": "RFC3339",
  "payload": {}
}
```

Core event kinds are `IssueCreated`, `MessageAppended`, `SnapshotRequested`, `SnapshotCompleted`, `AnalysisRequested`, `AnalysisCompleted`, `HandoffRequested`, `HandoffDelivered`, `SolutionSubmitted`, `ReporterConfirmed`, and `IssueReopened`.

## Initial configuration

```yaml
business_repositories:
  - name: string
    git_url: string
    local_path: absolute-path
ros_topology:
  expected_topology_file: absolute-path
log_paths:
  - absolute-path
```

`business_repositories` must contain at least one item. `ros_topology` is optional. Unknown top-level fields are rejected so site configuration cannot expand Codex or collector permissions.

## Hard security invariant

No configuration, prompt, message, plugin, or model output can grant write access to a configured business repository. Snapshot and Codex workers may only read those paths.
