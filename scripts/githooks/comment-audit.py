#!/usr/bin/env python3
"""Pre-commit comment audit: list every comment and docstring a commit's hunks touch,
kept lines included, sorted by whether their shape is on the allowlist, and ask.
Non-interactive callers are blocked — an agent does not approve its own prose."""
import ast
import os
import re
import subprocess
import sys

TAGLINE_MAX = 3
BLOCK_MAX = 2

COMMENT_RES = {
    (".py",): re.compile(r"(^\s*#)|(\s#\s)"),
    (".js", ".jsx", ".ts", ".tsx"): re.compile(
        r"(^\s*//)|(\s//\s)|(/\*)|(\*/)|(^\s*\*\s)"
    ),
    (".css",): re.compile(r"/\*"),
    (".html",): re.compile(r"<!--"),
}
PURE_COMMENT_RES = {
    (".py",): re.compile(r"^\s*#"),
    (".js", ".jsx", ".ts", ".tsx"): re.compile(r"^\s*(//|/\*|\*)"),
    (".css",): re.compile(r"^\s*/\*"),
    (".html",): re.compile(r"^\s*<!--"),
}

ALLOWLIST = """
ALLOWED — three shapes, nothing else:
  1. ONE line on a function, only when the name and signature do not carry it.
  2. A file tagline: what this file is, at the top, 1-3 lines.
  3. A magic number's provenance: its source, when the number cannot be
     reasoned about from the code alone.

DELETE everything else. Most often: a line restating the declaration under it,
a module docstring grown into paragraphs, a measurement or incident behind a
choice, and any contrast with what the code replaced. Those go in the commit
message, or nowhere.

A shape being allowed is not a pass — an allowed shape that says what the code
already says is still deleted.
"""

AGENT_VERDICT = """
If you are an agent reading this: "these ones are fine" is not available to you.
That judgement is what this hook exists to take away. For each line above, name
which of the three allowed shapes it is, or delete it. Default to deleting.
Re-stage and commit. Do not pass --no-verify, and do not ask for this hook to be
disabled.
"""


def pattern_for(path, table=COMMENT_RES):
    ext = os.path.splitext(path)[1]
    for exts, rx in table.items():
        if ext in exts:
            return rx
    return None


def staged_blob(path):
    out = subprocess.run(["git", "show", f":{path}"], capture_output=True, text=True)
    return out.stdout if out.returncode == 0 else ""


def docstring_spans(path, blob):
    if not path.endswith(".py"):
        return []
    try:
        tree = ast.parse(blob)
    except (SyntaxError, ValueError):
        return []
    spans = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            spans.append((first.lineno, first.end_lineno or first.lineno,
                          isinstance(node, ast.Module)))
    return spans


def block_range(lines, lineno, rx):
    """1-indexed [start, end] of the contiguous whole-line comments around a line, or None."""
    if rx is None or not 1 <= lineno <= len(lines) or not rx.match(lines[lineno - 1]):
        return None
    start = end = lineno
    while start > 1 and rx.match(lines[start - 2]):
        start -= 1
    while end < len(lines) and rx.match(lines[end]):
        end += 1
    return start, end


def classify(path, lineno, blob, spans):
    for start, end, is_module in spans:
        if start <= lineno <= end:
            size = end - start + 1
            if is_module:
                return ("tagline", size <= TAGLINE_MAX,
                        f"module docstring, {size} lines")
            return ("docstring", size == 1, f"docstring, {size} lines")
    lines = blob.splitlines()
    span = block_range(lines, lineno, pattern_for(path, PURE_COMMENT_RES))
    size = span[1] - span[0] + 1 if span else 1
    return ("comment", size <= BLOCK_MAX, f"comment block, {size} lines")


def touched_prose():
    diff = subprocess.run(
        ["git", "diff", "--cached", "--unified=0", "--no-color"],
        capture_output=True, text=True, check=True,
    ).stdout
    touched = {}
    path = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
            if path is not None:
                touched.setdefault(path, set())
        elif line.startswith("@@") and path is not None:
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            touched[path].update(range(start, start + count) if count else (start, start + 1))
    found = []
    for path, linenos in touched.items():
        rx = pattern_for(path)
        blob = staged_blob(path)
        lines = blob.splitlines()
        spans = docstring_spans(path, blob)
        pure = pattern_for(path, PURE_COMMENT_RES)
        prose = set()
        for n in linenos:
            for s0, e0, _ in spans:
                if s0 <= n <= e0:
                    prose.update(range(s0, e0 + 1))
            block = block_range(lines, n, pure)
            if block:
                prose.update(range(block[0], block[1] + 1))
            elif rx and 1 <= n <= len(lines) and rx.search(lines[n - 1]):
                prose.add(n)
        for n in sorted(prose):
            text = lines[n - 1] if n <= len(lines) else ""
            if text.strip():
                kind, ok, why = classify(path, n, blob, spans)
                found.append((path, n, text.strip(), kind, ok, why))
    return found


def report(rows, header):
    if not rows:
        return
    print(f"\n{header}\n", file=sys.stderr)
    for path, lineno, text, kind, _, why in rows:
        print(f"  {path}:{lineno}  [{kind}: {why}]", file=sys.stderr)
        print(f"    {text}", file=sys.stderr)


def main():
    prose = touched_prose()
    if not prose:
        return 0

    over = [r for r in prose if not r[4]]
    within = [r for r in prose if r[4]]
    report(over, f"{len(over)} OVER BUDGET — no allowed shape is this long:")
    report(within, f"{len(within)} within an allowed shape — does each say "
                   f"what the code cannot?")
    print(ALLOWLIST, file=sys.stderr)

    try:
        with open("/dev/tty") as tty:
            print("Keep them and commit? [y/N] ", end="", file=sys.stderr, flush=True)
            answer = tty.readline().strip().lower()
        return 0 if answer in ("y", "yes") else 1
    except OSError:
        print(AGENT_VERDICT, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
