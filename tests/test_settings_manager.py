import json
from pathlib import Path

import pytest

from config.settings_manager import SEATS, _merge, settings_manager, validate_llm, validate_seats


def test_merge_fills_keys_the_file_omits():
    defaults = {"working_directory": "outputs", "workqueue": {"token": "", "lease_seconds": 120}}
    merged = _merge(defaults, {"workqueue": {"token": "t"}})
    assert merged["working_directory"] == "outputs"
    assert merged["workqueue"] == {"token": "t", "lease_seconds": 120}


def test_merge_lets_the_file_win():
    merged = _merge({"a": 1, "b": {"c": 1}}, {"a": 2, "b": {"c": 2}})
    assert merged == {"a": 2, "b": {"c": 2}}


def test_example_settings_yield_every_key_callers_subscript():
    """CI bootstraps from settings.example.json; the example omits working_directory and every
    caller that subscripts it would KeyError."""
    example = json.loads(
        (Path(__file__).parent.parent / "src" / "config" / "settings.example.json").read_text())
    merged = _merge(settings_manager.defaults, example)
    assert merged["working_directory"]
    validate_llm(merged["llm"])
    validate_seats(merged["seats"], merged["llm"])
    assert merged["llm"]["qwen-flash-next"]["n_ctx"]
    assert merged["scaler"]["max_workers"]


PRICES = {"input": 0.28, "cached_input": 0.028, "output": 0.42}
SELF_HOSTED = {"model": "m", "n_ctx": 131072, "self_hosted": {"providers": {"runpod": {}, "aws": {}}}}


@pytest.mark.parametrize("name,entry,error", [
    ("qwen-flash-next", SELF_HOSTED, None),
    ("deepseek-4.1", {"model": "d", "n_ctx": 1, "api": {"base_url": "u", "usd_per_mtok": PRICES}},
     None),
    ("local", {"model": "l", "n_ctx": 1, "local_gpu": {}}, None),
    ("none-of-them", {"model": "m", "n_ctx": 1}, "exactly one of"),
    ("two-of-them", {**SELF_HOSTED, "local_gpu": {}}, "exactly one of"),
    ("Qwen_Flash", SELF_HOSTED, "a name is"),
    ("a" * 33, SELF_HOSTED, "a name is"),
    ("no-model", {"n_ctx": 1, "local_gpu": {}}, "needs model and n_ctx"),
    ("no-window", {"model": "m", "local_gpu": {}}, "needs model and n_ctx"),
    ("unpriced", {"model": "d", "n_ctx": 1, "api": {"base_url": "u"}}, "usd_per_mtok"),
    ("half-priced", {"model": "d", "n_ctx": 1,
                     "api": {"base_url": "u", "usd_per_mtok": {"input": 1, "output": 1}}},
     "usd_per_mtok"),
    ("no-url", {"model": "d", "n_ctx": 1, "api": {"usd_per_mtok": PRICES}}, "base_url"),
    ("gcp", {**SELF_HOSTED, "self_hosted": {"providers": {"gcp": {}}}}, "unknown providers"),
])
def test_an_llm_entry_is_checked_at_settings_load(name, entry, error):
    if error is None:
        validate_llm({name: entry})
        return
    with pytest.raises(ValueError, match=error) as e:
        validate_llm({name: entry})
    assert f"llm.{name}" in str(e.value)


EVERY_SEAT = {seat: ["qwen-flash-next"] for seat in SEATS}


@pytest.mark.parametrize("seats,error", [
    (EVERY_SEAT, None),
    ({**EVERY_SEAT, "build": ["deepseek-4.1", "qwen-flash-next"]}, None),
    ({k: v for k, v in EVERY_SEAT.items() if k != "gate"}, "seats.gate: a seat is"),
    ({**EVERY_SEAT, "design": []}, "seats.design: a seat is"),
    ({**EVERY_SEAT, "build": "qwen-flash-next"}, "seats.build: a seat is"),
    ({**EVERY_SEAT, "assets": ["opus"]}, r"seats.assets: no llm entry \['opus'\]"),
    ({**EVERY_SEAT, "audio": ["qwen-flash-next"]}, r"unknown seats \['audio'\]"),
])
def test_every_seat_names_llm_entries_settings_has(seats, error):
    llm = {"qwen-flash-next": SELF_HOSTED,
           "deepseek-4.1": {"model": "d", "n_ctx": 1, "api": {"base_url": "u", "usd_per_mtok": PRICES}}}
    if error is None:
        validate_seats(seats, llm)
        return
    with pytest.raises(ValueError, match=error):
        validate_seats(seats, llm)
