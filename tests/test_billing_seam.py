"""T4 buy-credits seam: the admin grant CLI and the provider webhook.

CLI grant credits a real handle (balance up, txn logged) and fails cleanly on an unknown handle.
The webhook completes a purchase on a (stubbed) verified event, rejects an unverified one, and is
NOT blocked by the user-auth middleware — a provider posts server-to-server with no user token.
"""

import pytest

from auth import cli, store
from billing import ledger
from billing.packages import PACKAGES
from billing.stripe import CreditProvider, PurchaseEvent
from db import connection


def _ledger_rows(user_id, reason):
    with connection.auth_db() as conn:
        return conn.execute(
            "SELECT delta FROM credit_transactions WHERE user_id = ? AND reason = ?",
            (user_id, reason),
        ).fetchall()


def test_cli_grant_increments_balance_and_logs_a_transaction(capsys):
    user = store.create_user("alice", "pw-pass1234", email="alice@example.com")
    assert cli.main(["grant", "alice", "50"]) == 0
    assert ledger.balance(user.id) == 50
    rows = _ledger_rows(user.id, "admin_grant")
    assert [r["delta"] for r in rows] == [50]
    assert "balance=" in capsys.readouterr().out


def test_cli_grant_on_unknown_handle_fails_cleanly():
    with pytest.raises(SystemExit) as exc:
        cli.main(["grant", "ghost", "50"])
    assert exc.value.code  # non-zero exit, clean message (not a traceback)
    assert "ghost" in str(exc.value.code)


class _StubProvider(CreditProvider):
    """Verifies iff the payload carries a matching secret — stands in for signature checking."""
    def __init__(self, purchase_id, secret=b"ok"):
        self._purchase_id, self._secret = purchase_id, secret

    def start_checkout(self, purchase_id, package, success_url, cancel_url):
        raise AssertionError("the webhook path never opens a checkout")

    def confirm_checkout(self, provider_ref):
        raise AssertionError("the webhook path never confirms a checkout")

    def verify(self, payload, headers):
        if payload != self._secret:
            return None
        return PurchaseEvent(purchase_id=self._purchase_id)


def _started_purchase(user_id):
    pkg = PACKAGES[1]
    purchase = ledger.create_purchase(user_id, pkg.id, pkg.credits, pkg.usd_cents)
    ledger.set_purchase_ref(purchase.id, "cs_test_ref")
    return purchase


def test_webhook_completes_the_purchase_on_a_verified_event(app_client, monkeypatch):
    user = store.create_user("alice", "pw-pass1234", email="alice2@example.com")
    purchase = _started_purchase(user.id)
    import api.routers.billing as billing_router
    stub = _StubProvider(purchase.id)
    monkeypatch.setattr(billing_router, "get_provider", lambda: stub)

    r = app_client.post("/api/billing/webhook", content=b"ok")
    assert r.status_code == 200
    assert ledger.balance(user.id) == purchase.credits
    assert [row["delta"] for row in _ledger_rows(user.id, "purchase")] == [purchase.credits]

    # A webhook redelivery (or the redirect-return racing it) grants nothing more.
    app_client.post("/api/billing/webhook", content=b"ok")
    assert ledger.balance(user.id) == purchase.credits


def test_webhook_rejects_an_unverified_event_and_credits_nothing(app_client, monkeypatch):
    user = store.create_user("alice", "pw-pass1234", email="alice3@example.com")
    purchase = _started_purchase(user.id)
    import api.routers.billing as billing_router
    stub = _StubProvider(purchase.id)
    monkeypatch.setattr(billing_router, "get_provider", lambda: stub)

    r = app_client.post("/api/billing/webhook", content=b"tampered")
    assert r.status_code == 400
    assert ledger.balance(user.id) == 0
    assert _ledger_rows(user.id, "purchase") == []


def test_webhook_is_not_blocked_by_the_user_auth_gate(app_client, monkeypatch):
    # No Authorization header — a gated route 401s, but the webhook reaches its handler (here a
    # clean 400 from the provider refusing) rather than being turned away at the gate.
    import api.routers.billing as billing_router
    stub = _StubProvider("nothing", secret=b"never")
    monkeypatch.setattr(billing_router, "get_provider", lambda: stub)
    assert app_client.get("/api/games").status_code == 401
    r = app_client.post("/api/billing/webhook", content=b"anything")
    assert r.status_code == 400
