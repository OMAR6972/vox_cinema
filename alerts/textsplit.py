"""Long messages are split into several, never cut off: a message that does not fit would lose whatever comes last."""
from __future__ import annotations


def split_message(text: str, limit: int) -> list:
    """Cut `text` into pieces of at most `limit` characters, only at blank lines (between day blocks and sections),
    so formatting tags are never torn in half. A single block that is still too long is cut at line ends."""
    if len(text) <= limit:
        return [text]
    pieces, cur = [], ""
    for block in text.split("\n\n"):
        for part in _lines_if_needed(block, limit):
            add = part if not cur else cur + "\n\n" + part
            if len(add) <= limit:
                cur = add
            else:
                pieces.append(cur)
                cur = part
    if cur:
        pieces.append(cur)
    return pieces


def _lines_if_needed(block: str, limit: int) -> list:
    if len(block) <= limit:
        return [block]
    out, cur = [], ""
    for line in block.split("\n"):
        add = line if not cur else cur + "\n" + line
        if len(add) <= limit:
            cur = add
        else:
            if cur:
                out.append(cur)
            cur = line[:limit]
    if cur:
        out.append(cur)
    return out
