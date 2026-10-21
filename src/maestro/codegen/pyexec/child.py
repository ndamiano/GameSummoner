"""The confined side: Landlock and the seccomp filter, then the model's program in the game folder,
with each tool a stub over the socket. No confinement, no program."""

from __future__ import annotations

import json  # noqa: F401
import math  # noqa: F401
import random  # noqa: F401
import re  # noqa: F401
import itertools  # noqa: F401
import collections  # noqa: F401
import statistics  # noqa: F401
import string  # noqa: F401
import textwrap  # noqa: F401
import functools  # noqa: F401
import operator  # noqa: F401
import copy  # noqa: F401
import heapq  # noqa: F401
import bisect  # noqa: F401
import difflib  # noqa: F401
import hashlib  # noqa: F401
import unicodedata  # noqa: F401
import datetime  # noqa: F401
import decimal  # noqa: F401
import fractions  # noqa: F401
import base64  # noqa: F401
import io
import os
import socket
import sys
import traceback

from maestro.codegen.pyexec import landlock, rpc, seccomp

TOOLS = {
    "list_files": (),
    "read_file": ("path", "offset", "lines"),
    "write_file": ("path", "content"),
    "edit_file": ("path", "old_text", "new_text"),
    "generate_media": ("id", "kind", "subject", "style", "details"),
    "compose_world": ("description", "seed"),
    "check_syntax": ("paths",),
    "play": ("js", "seconds"),
    "check_off": ("rows",),
    "done": ("summary",),
}


def _stub(sock, name):
    names = TOOLS[name]

    def call(*args, **kwargs):
        if len(args) > len(names):
            raise TypeError(f"{name}() takes at most {len(names)} arguments "
                            f"({', '.join(names) or 'none'}) but {len(args)} were given")
        for key, value in zip(names, args):
            if key in kwargs:
                raise TypeError(f"{name}() got two values for {key!r}")
            kwargs[key] = value
        rpc.send(sock, {"tool": name, "kwargs": kwargs})
        reply = rpc.recv(sock)
        if reply is None:
            raise SystemExit("the session ended while a tool call was in flight")
        return reply.get("value")
    call.__name__ = name
    return call


def main() -> int:
    sock = socket.socket(fileno=int(os.environ["PYEXEC_FD"]))
    code = sys.stdin.read()
    root = os.environ["PYEXEC_ROOT"]

    try:
        landlock.confine(root)
        os.chdir(root)
        seccomp.install()
    except Exception as e:
        print(f"pyexec: could not confine the process: {e}", file=sys.stderr)
        return 3

    for name in ("socket", "subprocess", "ctypes", "tempfile", "platform", "inspect", "pickle",
                 "webbrowser", "urllib", "http"):
        sys.modules.pop(name, None)

    out = io.StringIO()
    ns = {"__name__": "__main__", "__builtins__": __builtins__}
    ns.update({name: _stub(sock, name) for name in TOOLS})

    status = 0
    sys.stdout = out
    try:
        exec(compile(code, "<program>", "exec"), ns)
    except SystemExit:
        pass
    except BaseException:
        tb = traceback.format_exc()
        keep = [line for line in tb.splitlines(keepends=True)
                if "pyexec/child.py" not in line and "in main" not in line]
        out.write("\n" + "".join(keep))
        status = 1
    finally:
        sys.stdout = sys.__stdout__

    rpc.send(sock, {"done": True, "stdout": out.getvalue(), "status": status})
    sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
