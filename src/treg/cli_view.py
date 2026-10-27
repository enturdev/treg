"""What the CLI prints for an agent: data as TOON (https://toonformat.dev), then the next commands.

Every reader of this CLI is a coding agent. A view is a plain dict; `render` encodes it with the
reference TOON encoder and appends `help[N]:`, the runnable next commands as a TOON list, one per
line (https://axi.md)."""

from __future__ import annotations

import json
from typing import Any


def table(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    """Rows with exactly `fields`, in that order, so TOON writes them as one table: the encoder
    falls back to a list of objects as soon as one row lacks a key."""
    return [{f: row.get(f) for f in fields} for row in rows]


def render(view: dict[str, Any], help_lines: list[str] | None = None) -> str:
    """Consecutive one-line fields form one paragraph; each table or nested block is its own,
    with a blank line between paragraphs (valid TOON: a decoder skips them)."""
    from toon_format import encode

    paragraphs: list[str] = []
    lines: list[str] = []
    for key, value in view.items():
        if value is None:
            continue
        text = encode({key: value})
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
        paragraphs.append("\n".join([f"help[{len(help_lines)}]:"]
                                    + [f"  - {encode({'h': line})[len('h: '):]}" for line in help_lines]))
    return "\n\n".join(paragraphs)


def render_json(view: dict[str, Any], help_lines: list[str] | None = None) -> str:
    return json.dumps({**{k: v for k, v in view.items() if v is not None},
                       **({"help": help_lines} if help_lines else {})}, ensure_ascii=False)
