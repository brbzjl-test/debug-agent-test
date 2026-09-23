from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from .snapshot import SnapshotReport


def build_evidence_bundle(
    report: SnapshotReport,
    issue_id: str,
    summary: str,
    conversation: Sequence[Mapping[str, Any]],
) -> Path:
    """Package one immutable snapshot and its conversation into a portable ZIP."""
    manifest_path = Path(report.manifest_path).expanduser().resolve()
    snapshot_dir = manifest_path.parent
    snapshot_id = str(getattr(report, "snapshot_id", "snapshot"))
    bundle_path = snapshot_dir / "{}_{}_evidence.zip".format(issue_id, snapshot_id)
    temporary = bundle_path.with_suffix(".zip.tmp")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {"issue_id": issue_id, "snapshot_id": snapshot_id, "evidence": []}

    files: list[tuple[Path, str]] = []
    evidence = manifest.get("evidence", []) if isinstance(manifest, dict) else []
    if not isinstance(evidence, list):
        evidence = []
    else:
        for item in evidence:
            if not isinstance(item, dict) or not item.get("output_file"):
                continue
            source = Path(str(item["output_file"])).expanduser().resolve()
            try:
                relative = source.relative_to(snapshot_dir)
            except ValueError:
                continue
            if source.is_file():
                archive_name = relative.as_posix()
                item["archive_path"] = archive_name
                files.append((source, archive_name))
            item.pop("output_file", None)

    ok_count = sum(1 for item in evidence if isinstance(item, dict) and item.get("ok"))
    failed_count = sum(1 for item in evidence if isinstance(item, dict) and not item.get("ok"))
    readme = (
        "# 现场问题证据包\n\n"
        "- 问题 ID：{}\n"
        "- Snapshot ID：{}\n"
        "- 采集成功：{} 项\n"
        "- 采集失败或缺失：{} 项\n\n"
        "## 摘要\n\n{}\n\n"
        "`manifest.json` 供程序读取，`conversation.json` 保存转人工前的对话，"
        "`evidence/` 保存原始采集结果。\n"
    ).format(issue_id, snapshot_id, ok_count, failed_count, summary)
    conversation_json = json.dumps(
        {"issue_id": issue_id, "messages": list(conversation)},
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    manifest_json = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"

    checksums = []
    generated = {
        "摘要.md": readme.encode("utf-8"),
        "manifest.json": manifest_json.encode("utf-8"),
        "conversation.json": conversation_json.encode("utf-8"),
    }
    for name, data in generated.items():
        checksums.append("{}  {}".format(hashlib.sha256(data).hexdigest(), name))
    for source, archive_name in files:
        checksums.append("{}  {}".format(hashlib.sha256(source.read_bytes()).hexdigest(), archive_name))

    snapshot_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name, data in generated.items():
                archive.writestr(name, data)
            for source, archive_name in files:
                archive.write(source, archive_name)
            archive.writestr("SHA256SUMS.txt", "\n".join(checksums) + "\n")
        os.replace(str(temporary), str(bundle_path))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return bundle_path
