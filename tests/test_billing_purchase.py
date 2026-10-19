"""The credits storefront: the package catalog, start→complete through the provider seam, and the
purchase history. Completion grants through the ledger exactly once — a double-complete is
answered, not re-credited."""

import pytest

from auth import store
from billing import ledger
from billing.packages import PACKAGES
from billing.stripe import Checkout, CreditProvider
from db import connection


class _PaidProvider(CreditProvider):
    """A checkout that the provider reports as paid — what a completed Stripe session answers."""
    def start_checkout(self, purchase_id, package, success_url, cancel_url):
        return Checkout(ref=f"cs_{purchase_id}", url=f"https://pay.example/{purchase_id}")

    def confirm_checkout(self, provider_ref):
        return f"pi_{provider_ref}" if provider_ref.startswith("cs_") else None

    def verify(self, payload, headers):
        return None


@pytest.fixture(autouse=True)
def paid_provider(monkeypatch):
    import api.routers.billing as billing_router
    provider = _PaidProvider()
    monkeypatch.setattr(billing_router, "get_provider", lambda: provider)


def _authed_user(handle="alice"):
    user = store.create_user(handle, "pw-pass1234", email=f"{handle}@example.com")
    token = store.issue_token(user.id)
    return user, {"Authorization": f"Bearer {token}"}


def _purchase_ledger_rows(user_id):
    with connection.auth_db() as conn:
        return conn.execute(
            "SELECT delta FROM credit_transactions WHERE user_id = ? AND reason = 'purchase'",
            (user_id,),
        ).fetchall()


def test_the_price_list_is_public_and_buying_is_not(app_client):
    assert app_client.get("/api/billing/packages").status_code == 200
    assert app_client.post("/api/billing/purchase", json={"package_id": "one-credit"}).status_code == 401
    assert app_client.get("/api/billing/purchases").status_code == 401


def test_packages_list_the_catalog_at_flat_pricing(app_client):
    _, hdr = _authed_user()
    r = app_client.get("/api/billing/packages", headers=hdr)
    assert r.status_code == 200
    body = r.json()
    assert [(p["id"], p["credits"], p["usd_cents"]) for p in body["packages"]] == \
        [(p.id, p.credits, p.usd_cents) for p in PACKAGES]
    for p in body["packages"]:
        assert p["usd_cents"] == p["credits"] * 500


def test_purchase_grants_credits_exactly_once(app_client):
    user, hdr = _authed_user()

    started = app_client.post("/api/billing/purchase", json={"package_id": "five-credits"}, headers=hdr)
    assert started.status_code == 200
    assert started.json()["checkout_url"].startswith("https://pay.example/")
    assert ledger.balance(user.id) == 0

    done = app_client.post(
        f"/api/billing/purchase/{started.json()['purchase_id']}/complete", headers=hdr)
    assert done.status_code == 200
    assert done.json() == {"status": "completed", "credits": 5, "balance": 5}
    assert ledger.balance(user.id) == 5
    assert [r["delta"] for r in _purchase_ledger_rows(user.id)] == [5]


def test_double_complete_is_idempotent(app_client):
    user, hdr = _authed_user()
    pid = app_client.post("/api/billing/purchase", json={"package_id": "one-credit"},
                          headers=hdr).json()["purchase_id"]

    first = app_client.post(f"/api/billing/purchase/{pid}/complete", headers=hdr)
    second = app_client.post(f"/api/billing/purchase/{pid}/complete", headers=hdr)

    assert first.status_code == 200 and second.status_code == 200
    assert second.json() == {"status": "completed", "credits": 1, "balance": 1}
    assert ledger.balance(user.id) == 1
    assert [r["delta"] for r in _purchase_ledger_rows(user.id)] == [1]


def test_unknown_package_is_refused(app_client):
    user, hdr = _authed_user()
    r = app_client.post("/api/billing/purchase", json={"package_id": "100"}, headers=hdr)
    assert r.status_code == 404
    assert ledger.list_purchases(user.id) == []


def test_completing_another_users_purchase_is_refused(app_client):
    alice, alice_hdr = _authed_user("alice")
    _, mallory_hdr = _authed_user("mallory")
    pid = app_client.post("/api/billing/purchase", json={"package_id": "ten-credits"},
                          headers=alice_hdr).json()["purchase_id"]

    r = app_client.post(f"/api/billing/purchase/{pid}/complete", headers=mallory_hdr)
    assert r.status_code == 404
    assert ledger.balance(alice.id) == 0
    assert ledger.get_purchase(pid).status == "started"


def test_history_lists_purchases_newest_first(app_client):
    user, hdr = _authed_user()
    first = app_client.post("/api/billing/purchase", json={"package_id": "one-credit"},
                            headers=hdr).json()["purchase_id"]
    app_client.post(f"/api/billing/purchase/{first}/complete", headers=hdr)
    second = app_client.post("/api/billing/purchase", json={"package_id": "ten-credits"},
                             headers=hdr).json()["purchase_id"]
    app_client.post(f"/api/billing/purchase/{second}/complete", headers=hdr)

    r = app_client.get("/api/billing/purchases", headers=hdr)
    assert r.status_code == 200
    rows = r.json()
    assert [row["id"] for row in rows] == [second, first]
    assert [(row["credits"], row["usd_cents"], row["status"]) for row in rows] == \
        [(10, 5000, "completed"), (1, 500, "completed")]
    assert all(row["completed_at"] is not None for row in rows)


def test_history_is_scoped_to_the_caller(app_client):
    _, alice_hdr = _authed_user("alice")
    _, bob_hdr = _authed_user("bob")
    pid = app_client.post("/api/billing/purchase", json={"package_id": "one-credit"},
                          headers=alice_hdr).json()["purchase_id"]
    app_client.post(f"/api/billing/purchase/{pid}/complete", headers=alice_hdr)

    assert app_client.get("/api/billing/purchases", headers=bob_hdr).json() == []


def test_unpaid_checkout_never_grants(app_client, monkeypatch):
    """The provider's word, not the client's return visit, is what authorizes the grant."""
    class Unpaid(_PaidProvider):
        def confirm_checkout(self, provider_ref):
            return None

    import api.routers.billing as billing_router
    unpaid = Unpaid()
    monkeypatch.setattr(billing_router, "get_provider", lambda: unpaid)
    user, hdr = _authed_user("dave")
    pid = app_client.post("/api/billing/purchase", json={"package_id": "one-credit"},
                          headers=hdr).json()["purchase_id"]
    r = app_client.post(f"/api/billing/purchase/{pid}/complete", headers=hdr)
    assert r.status_code == 402
    assert ledger.balance(user.id) == 0


def test_checkout_returns_to_the_app_origin(app_client, monkeypatch):
    import api.routers.billing as billing_router
    from config.settings_manager import settings_manager
    real = settings_manager.get_settings()
    monkeypatch.setattr(settings_manager, "get_settings",
                        lambda: {**real, "play": {"app_origin": "https://app.example/"}})
    seen = {}

    class Capturing(_PaidProvider):
        def start_checkout(self, purchase_id, package, success_url, cancel_url):
            seen["success"] = success_url
            return super().start_checkout(purchase_id, package, success_url, cancel_url)

    capturing = Capturing()
    monkeypatch.setattr(billing_router, "get_provider", lambda: capturing)
    _, hdr = _authed_user("erin")
    app_client.post("/api/billing/purchase", json={"package_id": "one-credit"}, headers=hdr)
    assert seen["success"].startswith("https://app.example/credits?purchase=")


def _completed_purchase(app_client, hdr, package_id="five-credits"):
    pid = app_client.post("/api/billing/purchase", json={"package_id": package_id},
                          headers=hdr).json()["purchase_id"]
    app_client.post(f"/api/billing/purchase/{pid}/complete", headers=hdr)
    return pid


def test_completion_records_the_payment_intent(app_client):
    user, hdr = _authed_user("frank")
    pid = _completed_purchase(app_client, hdr)
    assert ledger.get_purchase(pid).payment_intent is not None


def test_a_refund_event_takes_the_credits_back_even_below_zero(app_client, monkeypatch):
    """The purchase's credits come back out on refund; spent credits mean a negative balance,
    and a negative balance blocks every purchase-priced action via the existing balance checks."""
    import api.routers.billing as billing_router
    from billing.stripe import RefundEvent

    user, hdr = _authed_user("grace")
    pid = _completed_purchase(app_client, hdr, package_id="one-credit")
    pi = ledger.get_purchase(pid).payment_intent
    ledger.deduct(user.id, 1, "build", None)

    class Refunding(_PaidProvider):
        def verify(self, payload, headers):
            return RefundEvent(payment_intent=pi) if payload == b"refund" else None

    refunding = Refunding()
    monkeypatch.setattr(billing_router, "get_provider", lambda: refunding)
    r = app_client.post("/api/billing/webhook", content=b"refund")
    assert r.status_code == 200 and r.json() == {"status": "refunded"}
    assert ledger.balance(user.id) == -1
    assert ledger.get_purchase(pid).status == "refunded"

    # Idempotent: a redelivered refund event revokes nothing more.
    app_client.post("/api/billing/webhook", content=b"refund")
    assert ledger.balance(user.id) == -1

    # Negative balance blocks the next purchase-priced action (the build path's balance check).
    assert app_client.post("/api/games", headers=hdr,
                           json={"prompt": "another game"}).status_code == 402


def test_history_hides_abandoned_checkouts(app_client):
    user, hdr = _authed_user("henry")
    app_client.post("/api/billing/purchase", json={"package_id": "one-credit"}, headers=hdr)
    done = _completed_purchase(app_client, hdr, package_id="five-credits")

    rows = app_client.get("/api/billing/purchases", headers=hdr).json()
    assert [r["id"] for r in rows] == [done]
