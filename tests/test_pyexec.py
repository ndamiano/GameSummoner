"""The sandbox a build's program runs in: what it may do, what it may not, and what it says.

The boundary is the seccomp filter, not the static check — so the tests that matter most are the
ones that hand a program straight to the runner and watch the kernel refuse it.
"""
import pytest

from maestro.codegen.pyexec import runner
from maestro.codegen.staging import seed_vendor
from maestro.codegen.tools import build_tools
from maestro.state import RunState


@pytest.fixture
def tools(tmp_path):
    seed_vendor(tmp_path)
    return build_tools(RunState(tmp_path), "b1")


def test_a_program_writes_reads_and_loops(tmp_path, tools):
    out = runner.run('''
for i in range(3):
    write_file(path=f"m{i}.js", content=f"export const x = {i};\\n")
names = sorted(f["path"] for f in list_files() if f["path"].startswith("m"))
print(names)
print(read_file(path="m1.js").strip())
''', tools)
    assert out.refused is None and not out.failed
    assert "['m0.js', 'm1.js', 'm2.js']" in out.stdout
    assert "export const x = 1;" in out.stdout
    assert (tmp_path / "game" / "m2.js").exists()


def test_a_failed_call_returns_its_error_and_the_rest_still_runs(tmp_path, tools):
    """A raise abandons every statement after it: one bad edit lost ten good ones (measured
    2026-09-07: 5 of 16 applied, 10 never attempted)."""
    out = runner.run('''
first = edit_file(path="nope.js", old_text="a", new_text="b")
print("first:", first[:5])
write_file(path="after.js", content="// still ran")
''', tools)
    assert out.stdout.startswith("first: ERROR")
    assert (tmp_path / "game" / "after.js").exists()
    assert out.ledger == [("edit_file", "nope.js", False), ("write_file", "after.js", True)]


def test_a_failed_play_carries_the_page_errors_that_explain_it(tmp_path, tools):
    out = runner.run('''
write_file("index.html", '<script type="module" src="./main.js"></script>')
write_file("main.js", 'import { actBoss } from "./enemies.js";\\nwindow.__game = {};\\n')
write_file("enemies.js", "export const other = 1;\\n")
print(play("return 1", seconds=1))
''', tools, timeout=60)
    assert out.stdout.startswith("ERROR: window.__game never appeared")
    assert "does not provide an export named 'actBoss'" in out.stdout


def test_the_path_jail_still_holds_inside_a_program(tmp_path, tools):
    out = runner.run('print(write_file(path="../../escape.js", content="x"))', tools)
    assert "escapes the project directory" in out.stdout
    assert not (tmp_path.parent / "escape.js").exists()


def test_a_traceback_names_the_program_and_not_the_harness(tmp_path, tools):
    out = runner.run('write_file(path="a.js", content="x")\nraise ValueError("boom")', tools)
    assert out.failed and 'File "<program>", line 2' in out.stdout
    assert "ValueError: boom" in out.stdout
    assert "child.py" not in out.stdout and "runner.py" not in out.stdout
    assert (tmp_path / "game" / "a.js").exists()      # what ran before the raise stands


def test_a_program_that_never_ends_is_stopped_and_told_so(tmp_path, tools):
    out = runner.run('write_file(path="a.js", content="x")\nwhile True:\n    pass', tools,
                     timeout=3)
    assert out.timed_out
    assert "was stopped" in out.stdout
    assert (tmp_path / "game" / "a.js").exists()      # the write before the loop stands


def test_the_static_check_refuses_a_reach_before_anything_runs(tmp_path, tools):
    out = runner.run('import subprocess\nwrite_file(path="never.js", content="x")', tools)
    assert "no module 'subprocess'" in out.refused
    assert "nothing in the program ran" in out.refused
    assert not (tmp_path / "game" / "never.js").exists()
    assert out.ledger == []


@pytest.mark.parametrize("code,says", [
    ('open("/etc/passwd")', "open()"),
    ('eval("1+1")', "eval()"),
    ('x = (1).__class__', "__class__"),
    ('from pathlib import Path', "no module 'pathlib'"),
    ('def f(:', "not valid Python"),
])
def test_what_the_static_check_names(code, says):
    assert says in runner.check(code)


def test_the_standard_library_a_design_needs_is_allowed():
    assert runner.check("import json, re, math, random, itertools, collections") is None


def test_the_filter_refuses_the_syscalls_the_static_check_did_not_see(tmp_path, tools, monkeypatch):
    """The static check is a courtesy; this is the boundary. With the check disabled, the kernel
    still refuses to open a file, reach the network or start a process."""
    monkeypatch.setattr(runner, "check", lambda code: None)
    out = runner.run('''
for what, thunk in [("open", lambda: open("/etc/passwd")),
                    ("socket", lambda: __import__("socket").socket()),
                    ("spawn", lambda: __import__("subprocess").run(["/bin/echo", "hi"]))]:
    try:
        thunk()
        print(what, "NOT BLOCKED")
    except Exception as e:
        print(what, "blocked", type(e).__name__)
''', tools)
    assert "NOT BLOCKED" not in out.stdout
    assert out.stdout.count("blocked") == 3


RAW_SYSCALLS = {
    "creat": "85, outside_new, 0o644",
    "truncate": "76, outside_old, 0",
    "io_uring_setup": "425, 1, ctypes.byref((ctypes.c_char * 120)())",
    "utimensat": "280, -100, outside_old, None, 0",
    "setxattr": "188, outside_old, b'user.x', b'1', 1, 0",
    "kill_parent": "62, os.getppid(), 0",
    "tgkill_parent": "234, os.getppid(), os.getppid(), 0",
}


def test_the_filter_refuses_raw_syscalls_that_reach_a_path_or_another_process(
        tmp_path, tools, monkeypatch):
    monkeypatch.setattr(runner, "check", lambda code: None)
    outside_old = tmp_path / "outside.txt"
    outside_old.write_text("precious")
    outside_new = tmp_path / "escaped.txt"
    calls = "\n".join(f'    ("{name}", lambda: libc.syscall({args})),'
                      for name, args in RAW_SYSCALLS.items())
    out = runner.run(f'''
import sys
ctypes = sys.modules["maestro.codegen.pyexec.seccomp"].ctypes
os = sys.modules["random"]._os
libc = ctypes.CDLL(None, use_errno=True)
outside_new, outside_old = {str(outside_new).encode()!r}, {str(outside_old).encode()!r}
for name, call in [
{calls}
]:
    print(name, "refused" if call() == -1 and ctypes.get_errno() == 1 else "ALLOWED")
''', tools)
    assert sorted(out.stdout.split()) == sorted(
        word for name in RAW_SYSCALLS for word in (name, "refused")), out.stdout
    assert not outside_new.exists()
    assert outside_old.read_text() == "precious"


def test_the_environment_carries_nothing_of_ours(tmp_path, tools, monkeypatch):
    monkeypatch.setattr(runner, "check", lambda code: None)
    monkeypatch.setenv("WORKER_TOKEN", "a-real-secret")
    out = runner.run('import os\nprint(sorted(os.environ))', tools)
    assert "WORKER_TOKEN" not in out.stdout


def test_a_program_calling_a_tool_forever_is_cut_off(tmp_path, tools):
    out = runner.run(f'''
for i in range({runner.MAX_CALLS + 50}):
    r = list_files()
print("made it to the end", r if isinstance(r, str) else "list")
''', tools)
    assert len(out.ledger) == runner.MAX_CALLS
    assert "more than a build turn should" in out.stdout


def test_a_tool_is_called_the_way_any_python_function_is(tmp_path, tools):
    """`read_file("game.js")` is what a model writes. Refusing it because the tools underneath take
    keyword arguments failed almost every program (measured 2026-09-07, on the first live turn)."""
    out = runner.run(
        'write_file("a.js", "const a = 1;\\n")\n'
        'print(read_file("a.js").strip())\n'
        'print(read_file("a.js", 1, 1).strip())\n'
        'edit_file("a.js", "1", "2")\n'
        'print(read_file(path="a.js").strip())\n', tools)
    assert out.stdout.splitlines() == ["const a = 1;", "const a = 1;", "const a = 2;"]
    assert (tmp_path / "game" / "a.js").read_text() == "const a = 2;\n"


def test_too_many_arguments_says_what_the_call_takes(tmp_path, tools):
    out = runner.run('read_file("a.js", 1, 2, 3)', tools)
    assert "takes at most 3 arguments (path, offset, lines)" in out.stdout


def test_a_tool_hands_the_program_the_thing_it_asked_for(tmp_path, tools):
    """The prompt says check_syntax() answers {path: verdict} and list_files() a list of files —
    so a program gets those, not the envelope the tool returns them in."""
    out = runner.run(
        'write_file("js/ok.js", "export const a = 1;\\n")\n'
        'write_file("js/bad.js", "export const = ;\\n")\n'
        'for path, verdict in sorted(check_syntax().items()):\n'
        '    print(path, "->", verdict)\n'
        'print(sorted(f["path"] for f in list_files() if f["path"].startswith("js/")))\n', tools)
    assert "js/bad.js -> line 1: Unexpected token '='" in out.stdout
    assert "js/ok.js -> OK" in out.stdout
    assert "['js/bad.js', 'js/ok.js']" in out.stdout
