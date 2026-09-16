# Debug Agent Test

A small macOS-friendly Python application used to verify field-support snapshot collection and read-only Codex analysis.

The application is intentionally independent from ROS and exposes no integration API to the support agent. It behaves like ordinary business software by writing stdout, stderr, rotating log files, and a runtime status file.

## Requirements

- Python 3.9 or newer
- macOS or Linux
- No third-party Python packages

## Scenarios

| Scenario | Behavior |
|-|-|
| `normal` | Emits healthy heartbeat logs and exits cleanly. |
| `device_missing` | Reports a missing device and fails during startup. |
| `software_conflict` | Reports conflicting control owners and rejected input. |
| `intermittent` | Briefly reports a fault, then recovers. |
| `dependency_error` | Simulates a missing runtime dependency. |
| `process_crash` | Raises an exception and writes a traceback. |
| `silent_hang` | Stops heartbeat output while the process remains alive. |

## Run

```bash
python3 app.py --scenario normal --duration 5
python3 app.py --scenario software_conflict --duration 8
python3 app.py --scenario intermittent --duration 10
```

Logs are written under `logs/` and current state under `runtime/`. Change them with `--log-dir` and `--runtime-dir`.

## Test

```bash
python3 -m unittest discover -s tests -v
```
