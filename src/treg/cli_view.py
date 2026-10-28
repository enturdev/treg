"""What the CLI prints for an agent: data as TOON (https://toonformat.dev), then the next commands.

Every reader of this CLI is a coding agent. A view is a plain dict; `render` encodes it with the
reference TOON encoder, except that a table is used only for several narrow rows: one row, or rows
wider than `TABLE_MAX_COLUMNS`, print field by field so each value stays beside its name. Commands
and request values (`VERBATIM_KEYS`, `help`) print as they are typed, unquoted, so they can be
copied (https://axi.md)."""

from __future__ import annotations

import json
from typing import Any

TABLE_MAX_COLUMNS = 8
VERBATIM_KEYS = frozenset({"call", "param_examples"})


def table(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    """Rows with exactly `fields`, in that order, so TOON writes them as one table: the encoder
    falls back to a list of objects as soon as one row lacks a key."""
    return [{f: row.get(f) for f in fields} for row in rows]


def _encode(value: dict[str, Any]) -> str:
    # toon-format exposes no option for when a list becomes a table, so the choice is made by
    # overriding its encoder's table decisions; tests round-trip the output through `decode`.
    from toon_format import _encoder as toon

    def readable(shape, rows: int) -> bool:
        def leaves(s) -> int:
            return sum(1 if nested is None else leaves(nested) for _, nested in s)
        return shape is not None and rows >= 2 and leaves(shape) <= TABLE_MAX_COLUMNS

    class Encoder(toon._Encoder):
        def array(self, prefix, array, depth, *, tabular):
            super().array(prefix, array, depth,
                          tabular=tabular and readable(toon._shape(array), len(array)))

        def field(self, key, value, depth):
            if isinstance(value, dict) and value and not readable(toon._keyed_shape(value), len(value)):
                self.emit(depth, key + ":")
                self.fields(value, depth + 1)
                return
            super().field(key, value, depth)

    encoder = Encoder(2, ",")
    encoder.fields(toon._Normalizer(None, False).normalize(value), 0)
    return "\n".join(encoder.lines)


def render(view: dict[str, Any], help_lines: list[str] | None = None) -> str:
    """Consecutive one-line fields form one paragraph; each table or nested block is its own,
    with a blank line between paragraphs (valid TOON: a decoder skips them)."""
    paragraphs: list[str] = []
    lines: list[str] = []
    for key, value in view.items():
        if value is None:
            continue
        text = _verbatim(key, value) if key in VERBATIM_KEYS else _encode({key: value})
        if "\n" not in text:
            lines.append(text)
            continue
        if lines:
            paragraphs.append("\n".join(lines))
            lines = []
        paragraphs.append(text)
    if lines:
        paragraphs.append("\n".join(lines))
    if help_lines:
        paragraphs.append("\n".join([f"help[{len(help_lines)}]:"] + [f"  - {_help_item(line)}" for line in help_lines]))
    return "\n\n".join(paragraphs)


def _verbatim(key: str, value: Any) -> str:
    """Values as typed; keys still follow TOON's quoting (`"person_titles[]"`), or a reader would take
    the brackets for an array header."""
    if isinstance(value, dict):
        return "\n".join([f"{_key(key)}:"] + [f"  {_key(k)}: {v}" for k, v in value.items()])
    return f"{_key(key)}: {value}"


def _key(key: str) -> str:
    from toon_format import encode
    return encode({key: 0})[:-len(": 0")]


def _help_item(line: str) -> str:
    """A command as typed; quoted only when `: ` would make a TOON reader take it for a field."""
    if ": " not in line:
        return line
    from toon_format import encode
    return encode({"h": line})[len("h: "):]


def render_json(view: dict[str, Any], help_lines: list[str] | None = None) -> str:
    return json.dumps({**{k: v for k, v in view.items() if v is not None},
                       **({"help": help_lines} if help_lines else {})}, ensure_ascii=False)
