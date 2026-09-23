"""Strict parser for the deliberately small site configuration format."""

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple


class ConfigError(ValueError):
    """Raised when a site configuration does not match the P0 contract."""


@dataclass(frozen=True)
class BusinessRepository:
    name: str
    git_url: str
    local_path: Path


@dataclass(frozen=True)
class RosTopology:
    expected_topology_file: Path


@dataclass(frozen=True)
class AppConfig:
    business_repositories: Tuple[BusinessRepository, ...]
    ros_topology: Optional[RosTopology] = None
    log_paths: Tuple[Path, ...] = ()


_KEY_VALUE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(?:\s*(.*))?$")


def _scalar(raw: str, line_number: int) -> str:
    value = raw.strip()
    if not value:
        raise ConfigError("line {}: value cannot be empty".format(line_number))
    if value[0:1] in ("'", '"'):
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError) as exc:
            raise ConfigError("line {}: invalid quoted value".format(line_number)) from exc
        if not isinstance(parsed, str) or not parsed:
            raise ConfigError("line {}: value must be a non-empty string".format(line_number))
        return parsed

    comment = re.search(r"\s+#", value)
    if comment:
        value = value[: comment.start()].rstrip()
    if not value or value.startswith(("&", "*", "!", "[", "{", ">", "|")):
        raise ConfigError("line {}: unsupported YAML value".format(line_number))
    return value


def _entry(line: str, indent: int, line_number: int) -> Tuple[str, str]:
    if "\t" in line:
        raise ConfigError("line {}: tabs are not allowed".format(line_number))
    actual_indent = len(line) - len(line.lstrip(" "))
    if actual_indent != indent:
        raise ConfigError("line {}: expected {} spaces of indentation".format(line_number, indent))
    match = _KEY_VALUE.match(line.strip())
    if not match or match.group(2) is None:
        raise ConfigError("line {}: expected key: value".format(line_number))
    return match.group(1), _scalar(match.group(2), line_number)


def _meaningful_lines(text: str) -> List[Tuple[int, str]]:
    result = []
    for number, raw_line in enumerate(text.splitlines(), 1):
        if "\t" in raw_line:
            raise ConfigError("line {}: tabs are not allowed".format(number))
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        result.append((number, raw_line.rstrip()))
    return result


def parse_config(text: str) -> AppConfig:
    """Parse only the YAML subset described by contracts/domain.md.

    Keeping the grammar narrow prevents configuration from becoming an
    accidental extension point for commands, model tools, or write access.
    """

    lines = _meaningful_lines(text)
    repositories = []
    topology = None
    log_paths = []
    seen_top = set()
    index = 0

    while index < len(lines):
        number, line = lines[index]
        if line.startswith(" "):
            raise ConfigError("line {}: unexpected indentation".format(number))
        match = _KEY_VALUE.match(line)
        if not match or (match.group(2) or "").strip():
            raise ConfigError("line {}: expected a top-level mapping".format(number))
        key = match.group(1)
        if key not in {"business_repositories", "ros_topology", "log_paths"}:
            raise ConfigError("line {}: unknown top-level field {!r}".format(number, key))
        if key in seen_top:
            raise ConfigError("line {}: duplicate field {!r}".format(number, key))
        seen_top.add(key)
        index += 1

        if key == "business_repositories":
            while index < len(lines) and lines[index][1].startswith("  -"):
                item_number, item_line = lines[index]
                if not item_line.startswith("  - "):
                    raise ConfigError("line {}: expected '- key: value'".format(item_number))
                first_key, first_value = _entry(item_line[4:], 0, item_number)
                values = {first_key: first_value}
                index += 1
                while index < len(lines) and lines[index][1].startswith("    "):
                    child_number, child_line = lines[index]
                    child_key, child_value = _entry(child_line, 4, child_number)
                    if child_key in values:
                        raise ConfigError("line {}: duplicate repository field {!r}".format(child_number, child_key))
                    values[child_key] = child_value
                    index += 1
                unknown = set(values) - {"name", "git_url", "local_path"}
                missing = {"name", "git_url", "local_path"} - set(values)
                if unknown:
                    raise ConfigError("line {}: unknown repository field {!r}".format(item_number, sorted(unknown)[0]))
                if missing:
                    raise ConfigError("line {}: missing repository field {!r}".format(item_number, sorted(missing)[0]))
                local_path = Path(values["local_path"])
                if not local_path.is_absolute():
                    raise ConfigError("line {}: local_path must be absolute".format(item_number))
                repositories.append(BusinessRepository(values["name"], values["git_url"], local_path))

        elif key == "ros_topology":
            if index >= len(lines) or not lines[index][1].startswith("  "):
                raise ConfigError("line {}: ros_topology cannot be empty".format(number))
            child_number, child_line = lines[index]
            child_key, child_value = _entry(child_line, 2, child_number)
            if child_key != "expected_topology_file":
                raise ConfigError("line {}: unknown ros_topology field {!r}".format(child_number, child_key))
            expected_path = Path(child_value)
            if not expected_path.is_absolute():
                raise ConfigError("line {}: expected_topology_file must be absolute".format(child_number))
            topology = RosTopology(expected_path)
            index += 1
            if index < len(lines) and lines[index][1].startswith("  "):
                raise ConfigError("line {}: unexpected ros_topology field".format(lines[index][0]))
        else:
            while index < len(lines) and lines[index][1].startswith("  -"):
                item_number, item_line = lines[index]
                if not item_line.startswith("  - "):
                    raise ConfigError("line {}: expected '- path'".format(item_number))
                value = _scalar(item_line[4:], item_number)
                path = Path(value)
                if not path.is_absolute():
                    raise ConfigError("line {}: log path must be absolute".format(item_number))
                log_paths.append(path)
                index += 1
            if not log_paths:
                raise ConfigError("log_paths must contain at least one item")

    if "business_repositories" not in seen_top or not repositories:
        raise ConfigError("business_repositories must contain at least one item")
    names = [repository.name for repository in repositories]
    paths = [str(repository.local_path) for repository in repositories]
    if len(names) != len(set(names)):
        raise ConfigError("repository names must be unique")
    if len(paths) != len(set(paths)):
        raise ConfigError("repository local paths must be unique")
    paths = [str(path) for path in log_paths]
    if len(paths) != len(set(paths)):
        raise ConfigError("log paths must be unique")
    return AppConfig(tuple(repositories), topology, tuple(log_paths))


def load_config(path: Path) -> AppConfig:
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError("cannot read configuration: {}".format(exc)) from exc
    return parse_config(content)
