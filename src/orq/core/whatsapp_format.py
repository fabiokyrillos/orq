"""Markdown to WhatsApp formatting, and long messages split for the phone (Phase 7.2)."""

from __future__ import annotations

import re

_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_UNDERLINE_BOLD = re.compile(r"__(.+?)__")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_TABLE_RULE = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _line(line: str) -> str:
    heading = _HEADING.match(line)
    if heading:
        line = f"*{_BOLD.sub(r'\1', heading.group(1))}*"
    line = _BOLD.sub(r"*\1*", line)
    line = _UNDERLINE_BOLD.sub(r"_\1_", line)
    return _LINK.sub(r"\1 (\2)", line)


def to_whatsapp(markdown: str) -> str:
    """Headings and **bold** to *bold*, __x__ to _x_, links to `text (url)`, table rows to bullets; code blocks kept."""
    out: list[str] = []
    in_code = False
    table_header = False
    for raw in markdown.splitlines():
        if raw.strip().startswith("```"):
            in_code = not in_code
            out.append(raw)
            continue
        if in_code:
            out.append(raw)
            continue
        stripped = raw.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            if _TABLE_RULE.match(stripped):
                continue
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if not table_header:  # the first row of a table is its header: the bullets carry the content
                table_header = True
                continue
            out.append("• " + " · ".join(_line(c) for c in cells if c))
            continue
        table_header = False
        out.append(_line(raw))
    return "\n".join(out).strip()


def _pieces(text: str, size: int) -> list[str]:
    """Chunks of at most `size` characters, cut at paragraph, then line, then hard boundaries."""
    chunks: list[str] = []
    current = ""
    for block in _blocks(text, size):
        joined = f"{current}\n\n{block}" if current else block
        if len(joined) <= size:
            current = joined
        else:
            chunks.append(current)
            current = block
    if current:
        chunks.append(current)
    return chunks


def _blocks(text: str, size: int) -> list[str]:
    blocks: list[str] = []
    for paragraph in text.split("\n\n"):
        if len(paragraph) <= size:
            blocks.append(paragraph)
            continue
        line_block = ""
        for line in paragraph.split("\n"):
            while len(line) > size:
                if line_block:
                    blocks.append(line_block)
                    line_block = ""
                blocks.append(line[:size])
                line = line[size:]
            joined = f"{line_block}\n{line}" if line_block else line
            if len(joined) <= size:
                line_block = joined
            else:
                blocks.append(line_block)
                line_block = line
        if line_block:
            blocks.append(line_block)
    return blocks


def split_message(text: str, limit: int = 3000) -> list[str]:
    """One message, or parts of at most `limit` characters each starting with `(i/n)`."""
    if len(text) <= limit:
        return [text]
    chunks = _pieces(text, limit - 12)  # room for "(nn/nn)\n"
    total = len(chunks)
    return [f"({i}/{total})\n{chunk}" for i, chunk in enumerate(chunks, 1)]
