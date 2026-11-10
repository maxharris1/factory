"""YAML loading for orch.yml and factory.md front matter."""

from __future__ import annotations

from dataclasses import dataclass

import yaml
from yaml.constructor import ConstructorError


@dataclass(frozen=True)
class Finding:
    severity: str
    message: str

    def format(self, path: str) -> str:
        if self.severity == "warning":
            return f"{path}: warning: {self.message}"
        if self.severity == "error":
            return f"{path}: {self.message}"
        raise ValueError(self.severity)


def is_strict_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys."""


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.Node, deep: bool = False) -> dict:
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def load_yaml(text: str) -> object:
    return yaml.load(text, Loader=UniqueKeyLoader)


def split_front_matter(text: str) -> tuple[str, str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing YAML front matter")
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            raw = "".join(lines[1:index])
            body = "".join(lines[index + 1 :])
            return raw, body
    raise ValueError("unterminated YAML front matter")
