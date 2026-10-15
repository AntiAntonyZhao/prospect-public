"""YAML loading with YAML 1.2 semantics.

PyYAML implements YAML 1.1, where unquoted `ON`, `OFF`, `YES`, `NO`, `Y`, `N`
are booleans — `provinces: [QC, ON]` would load as `["QC", True]`. In
YAML 1.2 they are strings; loading through ruamel's 1.2 safe loader keeps
every reader of the fact bank seeing the same values.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

# Plain scalars that YAML 1.1 parsers (PyYAML and friends) read as booleans.
YAML11_BOOLISH = {"y", "n", "yes", "no", "on", "off"}


def load_yaml_text(text: str) -> Any:
    return YAML(typ="safe", pure=True).load(StringIO(text))


def load_yaml(path: Path) -> Any:
    return load_yaml_text(path.read_text(encoding="utf-8"))


def yaml11_ambiguous_scalars(text: str) -> list[tuple[str, str]]:
    """(path, value) for unquoted scalars a YAML 1.1 parser would read as bool."""
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    doc = yaml.load(StringIO(text))
    found: list[tuple[str, str]] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        # Quoted scalars load as str subclasses; only exact `str` was plain.
        elif type(node) is str and node.lower() in YAML11_BOOLISH:
            found.append((path, node))

    walk(doc, "")
    return found
