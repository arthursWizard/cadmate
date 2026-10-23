"""Minimal reader for KiCad's S-expression files.

KiCad files are nested lists of atoms and quoted strings. This parser keeps the
character span of every list, so callers can edit one part of a file in place
and leave the rest byte for byte, whatever KiCad version wrote it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class SexprError(ValueError):
    pass


@dataclass
class Node:
    items: list["Node | str"] = field(default_factory=list)
    start: int = 0  # offset of "("
    end: int = 0  # offset just past ")"

    @property
    def head(self) -> str | None:
        first = self.items[0] if self.items else None
        return first if isinstance(first, str) else None

    def children(self, head: str) -> list["Node"]:
        return [i for i in self.items if isinstance(i, Node) and i.head == head]

    def child(self, head: str) -> "Node | None":
        found = self.children(head)
        return found[0] if found else None

    def atoms(self) -> list[str]:
        """The plain values after the head, e.g. (at 1 2 90) -> ["1", "2", "90"]."""
        return [i for i in self.items[1:] if isinstance(i, str)]

    def floats(self) -> list[float]:
        out = []
        for a in self.atoms():
            try:
                out.append(float(a))
            except ValueError:
                pass
        return out


def parse(text: str) -> Node:
    """Parse the first top-level list in text."""
    pos = 0
    n = len(text)
    stack: list[Node] = []
    root: Node | None = None
    while pos < n:
        c = text[pos]
        if c == "(":
            node = Node(start=pos)
            if stack:
                stack[-1].items.append(node)
            stack.append(node)
            pos += 1
        elif c == ")":
            if not stack:
                raise SexprError(f"unexpected ')' at offset {pos}")
            node = stack.pop()
            node.end = pos + 1
            pos += 1
            if not stack:
                root = node
                break
        elif c.isspace():
            pos += 1
        elif c == '"':
            pos, value = _read_string(text, pos)
            if not stack:
                raise SexprError(f"string outside a list at offset {pos}")
            stack[-1].items.append(value)
        else:
            start = pos
            while pos < n and not text[pos].isspace() and text[pos] not in '()"':
                pos += 1
            if not stack:
                raise SexprError(f"atom outside a list at offset {start}")
            stack[-1].items.append(text[start:pos])
    if root is None:
        raise SexprError("no complete list found")
    return root


def _read_string(text: str, pos: int) -> tuple[int, str]:
    out = []
    pos += 1
    while pos < len(text):
        c = text[pos]
        if c == "\\" and pos + 1 < len(text):
            nxt = text[pos + 1]
            out.append({"n": "\n", "t": "\t"}.get(nxt, nxt))
            pos += 2
        elif c == '"':
            return pos + 1, "".join(out)
        else:
            out.append(c)
            pos += 1
    raise SexprError("unterminated string")


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
