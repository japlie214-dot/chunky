"""Split a .sql script into statements.

A naive ``script.split(";")`` is wrong for this repo specifically: every
procedure DDL wraps a Python body in ``$$ ... $$``, and those bodies contain
semicolons, quotes and comments. Splitting on a bare semicolon corrupts them
into unrunnable fragments, and the resulting Snowflake error points at the
wrong line.

This tracks the four contexts where a ';' is not a statement terminator:
single quotes, double quotes, dollar-quoted blocks ($$ ... $$), and comments
(-- to end of line, and /* ... */, which Snowflake nests).
"""

from __future__ import annotations

from typing import List


def split_statements(sql: str) -> List[str]:
    """Return the non-empty statements in `sql`, terminators stripped."""
    statements: List[str] = []
    buf: List[str] = []

    i = 0
    n = len(sql)
    in_single = False
    in_double = False
    in_dollar = False
    in_line_comment = False
    block_depth = 0

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if block_depth:
            buf.append(ch)
            if ch == "/" and nxt == "*":
                buf.append(nxt)
                block_depth += 1
                i += 2
                continue
            if ch == "*" and nxt == "/":
                buf.append(nxt)
                block_depth -= 1
                i += 2
                continue
            i += 1
            continue

        if in_single:
            buf.append(ch)
            # Snowflake escapes a quote by doubling it, and also honours
            # backslash escapes inside string literals.
            if ch == "\\" and nxt:
                buf.append(nxt)
                i += 2
                continue
            if ch == "'":
                if nxt == "'":
                    buf.append(nxt)
                    i += 2
                    continue
                in_single = False
            i += 1
            continue

        if in_double:
            buf.append(ch)
            if ch == '"':
                if nxt == '"':
                    buf.append(nxt)
                    i += 2
                    continue
                in_double = False
            i += 1
            continue

        if in_dollar:
            if ch == "$" and nxt == "$":
                buf.append("$$")
                in_dollar = False
                i += 2
                continue
            buf.append(ch)
            i += 1
            continue

        # --- default context ---
        if ch == "-" and nxt == "-":
            in_line_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "*":
            block_depth = 1
            buf.append(ch)
            buf.append(nxt)
            i += 2
            continue
        if ch == "$" and nxt == "$":
            in_dollar = True
            buf.append("$$")
            i += 2
            continue
        if ch == "'":
            in_single = True
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double = True
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)

    # Drop fragments that are only comments/whitespace -- Snowflake rejects them.
    return [s for s in statements if _has_code(s)]


def _has_code(stmt: str) -> bool:
    """True if `stmt` contains anything other than comments and whitespace."""
    out: List[str] = []
    i, n = 0, len(stmt)
    depth = 0
    while i < n:
        ch = stmt[i]
        nxt = stmt[i + 1] if i + 1 < n else ""
        if depth:
            if ch == "*" and nxt == "/":
                depth -= 1
                i += 2
                continue
            i += 1
            continue
        if ch == "/" and nxt == "*":
            depth += 1
            i += 2
            continue
        if ch == "-" and nxt == "-":
            while i < n and stmt[i] != "\n":
                i += 1
            continue
        out.append(ch)
        i += 1
    return bool("".join(out).strip())
