"""YAML loading for untrusted documents: aliases are never expanded.

`yaml.safe_load` is safe against code execution but not against cost. An alias is a reference, so
nine levels of nine aliases each ("billion laughs") is 264 bytes on disk and millions of values
once loaded — 2.3M characters to embed from a `.yaml` file, or 3.1 MB of metadata JSON from a
Markdown file's front matter (docs/safety-considerations.md).

Here an alias loads as its own text (`*name`) instead of a copy of what it points at, so a
document costs what its bytes cost. A merge key (`<<: *base`) needs the mapping it names and so
fails to load, which the readers report as an unparseable file.
"""

from collections.abc import Iterator
from typing import Any

import yaml
from yaml.events import AliasEvent
from yaml.nodes import Node, ScalarNode


class _NoAliasLoader(yaml.SafeLoader):
    def compose_node(self, parent: Node | None, index: Any) -> Node | None:
        if self.check_event(AliasEvent):
            event = self.get_event()  # type: ignore[no-untyped-call]
            return ScalarNode(
                "tag:yaml.org,2002:str", f"*{event.anchor}", event.start_mark, event.end_mark
            )
        node: Node | None = super().compose_node(parent, index)
        return node


def load(text: str) -> Any:
    """One YAML document, with aliases left unexpanded."""
    return yaml.load(text, Loader=_NoAliasLoader)


def load_all(text: str) -> Iterator[Any]:
    """Every YAML document in a stream, with aliases left unexpanded."""
    yield from yaml.load_all(text, Loader=_NoAliasLoader)
