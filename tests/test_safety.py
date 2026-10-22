"""The Phase-1 safety screen: hard-illegal (CSAM-adjacent) content is refused fail-closed, while
mature-but-legal creative themes — explicitly in-scope product — always pass. Covers the two match
shapes (unambiguous term; minor-descriptor × sexual-term co-occurrence), the logging contract
(matched terms only, never the full text), and the new-game endpoint's refusal path."""

import logging

import pytest

from auth import store
from billing import ledger
from db import events, games
from maestro.codegen import build_chain
from tools.safety import (
    SafetyViolation,
    log_violation,
    screen_image_prompt,
    screen_text,
)


@pytest.mark.parametrize("text", [
    None,
    "",
    "make me a game about a space pirate",
    # Either half of the combination alone is fine — the block is the combination.
    "a 10-year-old's birthday party",
    "a school girl walks to class",
    "a steamy adult romance with explicit scenes",
    # Dark/violent fiction is in-scope product, not a safety category.
    "a brutal war story where the hero dies",
])
def test_clean_and_single_half_texts_pass(text):
    assert screen_text(text) is None


def test_word_boundaries_prevent_substring_false_positives():
    # "sex" inside "Essex", "kid" inside "kidney" — boundary-anchored patterns must not fire.
    assert screen_text("a detective in Essex investigates a kidney heist") is None


def test_unambiguous_term_blocks_regardless_of_context():
    v = screen_text("a game with child porn in it")
    assert v is not None
    assert v.category == "csam_explicit"
    assert v.matched.lower() == "child porn"


def test_unambiguous_match_is_case_insensitive():
    assert screen_text("CSAM").category == "csam_explicit"


def test_minor_plus_sexual_combination_blocks():
    v = screen_text("a nude schoolgirl")
    assert v is not None
    assert v.category == "csam_combination"
    # Both halves ride in `matched` so the log shows WHY without the full text.
    assert "schoolgirl" in v.matched and "nude" in v.matched


def test_numeric_age_pattern_counts_as_minor_descriptor():
    assert screen_text("an explicit scene with a 12 year old").category == "csam_combination"
    assert screen_text("sexual content, aged 9").category == "csam_combination"


def test_adult_ages_do_not_match_the_minor_pattern():
    # The age pattern caps at 17 — adult ages with sexual terms are legal content.
    assert screen_text("an explicit scene with an 18 year old") is None
    assert screen_text("a nude 25-year-old model") is None


@pytest.mark.parametrize("form", ["masturbating", "masturbation", "molested", "raping", "fondling"])
def test_inflected_sexual_terms_are_matched(form):
    """Regression: the term list once held bare stems ('masturbat') that the word-boundary
    compiler could never match against inflected forms."""
    assert screen_text(f"a teen {form}").category == "csam_combination"


def test_screen_image_prompt_is_the_same_screen():
    assert screen_image_prompt("child porn") is not None
    assert screen_image_prompt("a heroic knight, pixel art") is None


def test_log_violation_records_terms_and_user_but_never_the_text(caplog):
    full_text = "the full user request must never be persisted"
    v = SafetyViolation("csam_combination", "teen+nude")
    with caplog.at_level(logging.WARNING, logger="maestro.safety"):
        log_violation(v, user_id="u123", source="chat")
    record = caplog.records[-1].getMessage()
    assert "csam_combination" in record
    assert "u123" in record
    assert "teen+nude" in record
    assert full_text not in record


@pytest.fixture
def client(app_client):
    return app_client


def test_a_blocked_prompt_starts_no_build_and_costs_nothing(client, monkeypatch):
    """The screen runs before the run is created, so a blocked prompt spends no credit and no
    inference — and the violation is logged against the user."""
    def _explode(*a, **k):
        raise AssertionError("a blocked prompt must never reach the build")
    monkeypatch.setattr(build_chain, "kickoff", _explode)

    u = store.create_user("alice", "pw-pass1234", email="alice@example.com")
    ledger.grant(u.id, 5, "admin_grant")
    headers = {"Authorization": f"Bearer {store.issue_token(u.id)}"}

    r = client.post("/api/games", headers=headers, json={"prompt": "child porn game"})
    assert r.status_code == 400
    assert ledger.balance(u.id) == 5


def test_log_violation_persists_a_row_for_the_admin_panel():
    log_violation(SafetyViolation("csam_combination", "teen+nude"),
                  user_id="u123", source="new_game", run_id="r1")
    rows = events.list_violations()
    assert rows[0]["user_id"] == "u123"
    assert rows[0]["game_id"] == "r1"
    assert (rows[0]["source"], rows[0]["category"]) == ("new_game", "csam_combination")


@pytest.fixture
def owned_game(client, tmp_runs, monkeypatch):
    """A built run owned by a real user, created through the API with the build itself stubbed."""
    monkeypatch.setattr(build_chain, "kickoff", lambda *a, **k: "b1")
    u = store.create_user("bob", "pw-pass1234", email="bob@example.com")
    ledger.grant(u.id, 5, "admin_grant")
    headers = {"Authorization": f"Bearer {store.issue_token(u.id)}"}
    r = client.post("/api/games", headers=headers, json={"prompt": "a space pirate game"})
    assert r.status_code == 200
    return r.json()["run_id"], headers


def test_a_blocked_fix_note_is_refused_and_recorded(client, owned_game):
    run_id, headers = owned_game
    r = client.post(f"/api/games/{run_id}/change", headers=headers,
                    json={"note": "add a nude schoolgirl"})
    assert r.status_code == 400
    rows = events.list_violations()
    assert rows and rows[0]["source"] == "change_note" and rows[0]["game_id"] == run_id


def test_a_held_game_is_frozen(client, owned_game):
    run_id, headers = owned_game
    games.move(run_id, "held")
    for path, body in [("play-session", {}), ("build", {}), ("change", {"note": "make it fun"}),
                       ("resume", {}), ("assets", {})]:
        r = client.post(f"/api/games/{run_id}/{path}", headers=headers, json=body)
        assert r.status_code == 423, path
    detail = client.get(f"/api/games/{run_id}", headers=headers).json()
    assert detail["status"] == "held"
    assert detail["built"] is False
