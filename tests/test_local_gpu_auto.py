"""`scripts/local_gpu.py auto`'s decision function — pure, no process spawning."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("local_gpu", ROOT / "scripts" / "local_gpu.py")
local_gpu = importlib.util.module_from_spec(spec)
sys.modules["local_gpu"] = local_gpu
spec.loader.exec_module(local_gpu)


def test_nothing_pending_holds_nothing():
    assert local_gpu.choose_action(None, {"llm": 0, "comfy": 0}, {}, 3) is None


def test_starts_on_priority_order_when_nothing_held():
    pending = {"decide": 1, "llm": 0, "comfy": 2}
    assert local_gpu.choose_action(None, pending, {}, 3) == "decide"


def test_llm_wins_priority_over_image():
    pending = {"llm": 1, "comfy": 2}
    assert local_gpu.choose_action(None, pending, {}, 3) == "llm"


def test_held_queue_keeps_the_card_while_it_has_work():
    pending = {"llm": 1, "comfy": 5}
    idle_ticks = {"llm": 0, "comfy": 0}
    assert local_gpu.choose_action("llm", pending, idle_ticks, 3) == "llm"


def test_a_starving_queue_takes_the_card_from_a_busy_one():
    pending = {"llm": 1, "comfy": 5}
    waits = {"llm": 5.0, "comfy": local_gpu.STARVE_SECONDS + 1}
    assert local_gpu.choose_action("llm", pending, {"llm": 0}, 3, waits) == "comfy"
    # under the cut-off the held queue keeps the card
    waits["comfy"] = local_gpu.STARVE_SECONDS - 1
    assert local_gpu.choose_action("llm", pending, {"llm": 0}, 3, waits) == "llm"


def test_the_longest_starving_queue_wins_and_the_held_one_never_counts():
    pending = {"decide": 1, "llm": 1, "comfy": 2}
    waits = {"llm": 9999.0, "comfy": local_gpu.STARVE_SECONDS + 50,
             "decide": local_gpu.STARVE_SECONDS + 5}
    assert local_gpu.choose_action("llm", pending, {"llm": 0}, 3, waits) == "comfy"


def test_held_queue_keeps_card_through_a_momentary_empty():
    # a completion just fired; a continuation may land before the idle-tick limit
    pending = {"llm": 0, "comfy": 5}
    idle_ticks = {"llm": 1, "comfy": 0}
    assert local_gpu.choose_action("llm", pending, idle_ticks, 3) == "llm"


def test_held_queue_yields_once_drained_and_another_has_work():
    pending = {"llm": 0, "comfy": 5}
    idle_ticks = {"llm": 3, "comfy": 0}
    assert local_gpu.choose_action("llm", pending, idle_ticks, 3) == "comfy"


def test_held_queue_drained_and_nothing_else_pending_releases_the_card():
    # idle-exit timing is handled by the caller (auto loop); the decision function just
    # reports "nothing wants the card"
    pending = {"llm": 0, "comfy": 0}
    idle_ticks = {"llm": 3, "comfy": 0}
    assert local_gpu.choose_action("llm", pending, idle_ticks, 3) is None


def test_llm_leg_serves_the_settings_model_on_basalt_with_vision_and_its_flags(monkeypatch, tmp_path):
    config = tmp_path / "basalt.json"
    config.write_text("{}")
    monkeypatch.setattr(local_gpu, "BASALT_CONFIG", config)
    monkeypatch.setattr(local_gpu, "_settings",
                        lambda: {"llm": {"model": "fn-basalt", "basalt_args": "--parallel 2"}})
    argv = local_gpu._leg("llm")["argv"]
    assert argv[1:3] == ["serve", "--config"] and argv[3] == str(config)
    assert argv[argv.index("--alias") + 1] == "fn-basalt"
    assert argv[argv.index("--vision") + 1] == "dynamic"
    assert argv[-2:] == ["--parallel", "2"]
    config.unlink()
    with __import__("pytest").raises(SystemExit):
        local_gpu._leg("llm")


def test_a_handed_over_worker_is_given_time_to_finish_its_job(monkeypatch):
    """The job in flight is GPU seconds already paid for; killing it re-pays the whole prefill when
    the job is re-driven. The server may go promptly — nothing is mid-flight in it once the worker
    has stopped."""
    graces = {}
    monkeypatch.setattr(local_gpu, "_stop",
                        lambda name, proc, grace=25: graces.__setitem__(name.split("-")[-1], grace))
    local_gpu.hand_over("llm", object(), object())
    assert graces == {"worker": local_gpu.HANDOFF_SECONDS, "server": 25}
    assert local_gpu.HANDOFF_SECONDS > 120        # longer than a build turn


def test_a_server_auto_did_not_start_is_never_stopped(monkeypatch):
    """auto reuses a server already on the port, and that one belongs to whoever started it."""
    stopped = []
    monkeypatch.setattr(local_gpu, "_stop", lambda name, proc, grace=25: stopped.append(name))
    local_gpu.hand_over("comfy", None, object())
    assert stopped == ["comfy-worker"]
