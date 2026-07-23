from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import yaml
from yaml.events import AliasEvent
from yaml.nodes import MappingNode


MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_STRUCTURE_DEPTH = 48
MAX_STRUCTURE_NODES = 100_000
MAX_YAML_ALIASES = 32
MAX_TEXT_FIELD_BYTES = 2 * 1024 * 1024


class LimitedSafeLoader(yaml.SafeLoader):
    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._p2h_aliases = 0
        self._p2h_depth = 0
        self._p2h_nodes = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(AliasEvent):
            self._p2h_aliases += 1
            if self._p2h_aliases > MAX_YAML_ALIASES:
                raise ValueError(f"YAML alias limit exceeded ({MAX_YAML_ALIASES})")
        self._p2h_depth += 1
        if self._p2h_depth > MAX_STRUCTURE_DEPTH:
            raise ValueError(f"YAML nesting limit exceeded ({MAX_STRUCTURE_DEPTH})")
        try:
            node = super().compose_node(parent, index)
            self._p2h_nodes += 1
            if self._p2h_nodes > MAX_STRUCTURE_NODES:
                raise ValueError(f"YAML node limit exceeded ({MAX_STRUCTURE_NODES})")
            return node
        finally:
            self._p2h_depth -= 1

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, MappingNode):
            raise ValueError("YAML mapping node is invalid")
        self.flatten_mapping(node)
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise ValueError("YAML mapping contains an unhashable key") from exc
            if duplicate:
                raise ValueError(f"duplicate YAML mapping key: {key!r}")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def read_limited_text(path: Path, *, limit: int = MAX_METADATA_BYTES) -> str:
    size = path.stat().st_size
    if size > limit:
        raise ValueError(f"metadata file exceeds {limit} bytes: {path.name}")
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        raise ValueError(f"metadata file is not valid UTF-8: {path.name}") from exc


def load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        # LimitedSafeLoader subclasses yaml.SafeLoader and only adds stricter limits.
        value = yaml.load(  # nosec B506
            read_limited_text(path), Loader=LimitedSafeLoader
        )
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise ValueError(f"invalid YAML {path.name}: {exc}") from exc
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"YAML root must be a mapping: {path.name}")
    validate_structure(value, source=path.name)
    return value


def load_json_text(text: str, *, source: str) -> Any:
    if len(text.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError(f"JSON metadata exceeds {MAX_METADATA_BYTES} bytes: {source}")

    def reject_constant(value: str) -> Any:
        raise ValueError(f"non-finite JSON number: {value}")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_keys,
        )
    except (RecursionError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid JSON {source}: {exc}") from exc
    validate_structure(value, source=source)
    return value


def load_json_value_file(path: Path) -> Any:
    try:
        text = read_limited_text(path)
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid JSON {path.name}: {exc}") from exc
    return load_json_text(text, source=path.name)


def load_json_file(path: Path) -> dict[str, Any]:
    value = load_json_value_file(path)
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path.name}")
    return value


def validate_structure(value: Any, *, source: str) -> None:
    nodes = 0
    active: set[int] = set()

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_STRUCTURE_NODES:
            raise ValueError(f"metadata node limit exceeded in {source}")
        if depth > MAX_STRUCTURE_DEPTH:
            raise ValueError(f"metadata nesting limit exceeded in {source}")
        if isinstance(item, str) and len(item.encode("utf-8")) > MAX_TEXT_FIELD_BYTES:
            raise ValueError(
                f"metadata text field exceeds {MAX_TEXT_FIELD_BYTES} bytes in {source}"
            )
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError(f"metadata contains a non-finite number in {source}")
        if not isinstance(item, (dict, list)):
            return
        identity = id(item)
        if identity in active:
            raise ValueError(f"recursive metadata structure in {source}")
        active.add(identity)
        try:
            values = item.items() if isinstance(item, dict) else enumerate(item)
            for key, child in values:
                if isinstance(key, str) and len(key.encode("utf-8")) > 1024:
                    raise ValueError(f"metadata key is too long in {source}")
                visit(child, depth + 1)
        finally:
            active.remove(identity)

    visit(value, 0)
