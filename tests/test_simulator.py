from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from personal_investment_simulator.app import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_app(tmp_path / "test.sqlite3", seed=True)
    return TestClient(app)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def set_quote(client: TestClient, code: str, price: float) -> None:
    r = client.post("/api/quotes/manual", json={"code": code, "price": price, "observed_at": now_utc()})
    assert r.status_code == 200, r.text


def set_topix(client: TestClient, price: float = 1.844, iopv: float | None = None) -> None:
    set_quote(client, "513800", price)
    if iopv is not None:
        r = client.post("/api/iopv", json={"code": "513800", "iopv": iopv, "observed_at": now_utc()})
        assert r.status_code == 200, r.text


def test_empty_account_and_3000_deposit_are_not_double_counted(client: TestClient) -> None:
    assert client.get("/api/state").json()["account"]["nav"] == 0
    r = client.post("/api/deposit", json={"amount": 3000, "note": "月度投入"})
    assert r.status_code == 200
    account = client.get("/api/state").json()["account"]
    assert account["nav"] == 3000
    assert account["cash"] == 3000
    assert account["reserved_cash"] == 0
    assert account["units"] == 3000


def test_missing_iopv_blocks_topix_order_and_5_yuan_minimum_is_cost_block(client: TestClient) -> None:
    client.post("/api/deposit", json={"amount": 3000})
    set_topix(client, 1.844)
    r = client.post("/api/orders/preview", json={"code": "513800", "quantity": 100, "limit_price": 1.844})
    assert r.status_code == 200
    assert not r.json()["ok"]
    assert any("IOPV" in reason for reason in r.json()["reasons"])

    set_topix(client, 1.844, 1.8493)
    r = client.post("/api/orders/preview", json={"code": "513800", "quantity": 100, "limit_price": 1.844})
    assert not r.json()["ok"]
    assert r.json()["premium_pct"] == "-0.287"
    assert any("0.5%" in reason for reason in r.json()["reasons"])
    assert r.json()["fee"] == "5.00"


def test_topix_order_fill_uses_cash_and_iopv_evidence(client: TestClient) -> None:
    client.post("/api/deposit", json={"amount": 3000})
    client.post("/api/settings", json={"commission_rate": 0, "minimum_commission": 0})
    set_topix(client, 1.844, 1.8493)
    body = {"code": "513800", "quantity": 100, "limit_price": 1.844, "idempotency_key": "topix-order-1"}
    submitted = client.post("/api/orders", json=body)
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "pending_fill"
    account = client.get("/api/state").json()["account"]
    assert account["cash"] == 2815.6
    assert account["reserved_cash"] == 184.4
    filled = client.post(f"/api/orders/{submitted.json()['id']}/fill", json={})
    assert filled.status_code == 200, filled.text
    state = client.get("/api/state").json()
    assert state["account"]["nav"] == 3000
    assert state["positions"][0]["quantity"] == "100"


def test_topix_cap_is_checked_against_whole_account(client: TestClient) -> None:
    client.post("/api/deposit", json={"amount": 3000})
    client.post("/api/settings", json={"commission_rate": 0, "minimum_commission": 0})
    set_topix(client, 1.844, 1.8493)
    r = client.post("/api/orders/preview", json={"code": "513800", "quantity": 400, "limit_price": 1.844})
    assert not r.json()["ok"]
    assert any("topix" in reason and "20%" in reason for reason in r.json()["reasons"])


def test_fund_subscription_waits_for_nav_and_books_fee_once(client: TestClient) -> None:
    client.post("/api/deposit", json={"amount": 3000})
    client.post("/api/settings", json={"platform_012692_verified": True})
    preview = client.post("/api/orders/preview", json={"code": "012692", "amount": 1350}).json()
    assert preview["ok"]
    assert preview["fee"] == "0.67"
    order = client.post("/api/orders", json={"code": "012692", "amount": 1350}).json()
    before = client.get("/api/state").json()["account"]
    assert before["nav"] == 3000
    assert before["cash"] == 1650
    assert before["reserved_cash"] == 1350
    assert client.get("/api/state").json()["positions"] == []
    confirmed = client.post(f"/api/orders/{order['id']}/confirm", json={"nav": 1.0522, "nav_date": "2026-10-27", "evidence": "模拟平台确认凭据"})
    assert confirmed.status_code == 200, confirmed.text
    after = client.get("/api/state").json()
    assert after["orders"][0]["status"] == "filled"
    assert after["account"]["reserved_cash"] == 0
    assert after["account"]["nav"] == 2999.33
    assert len(after["positions"]) == 1


def test_017641_daily_quota_and_suspended_fund_are_enforced(client: TestClient) -> None:
    client.post("/api/deposit", json={"amount": 3000})
    client.post("/api/settings", json={"platform_017641_verified": True})
    first = client.post("/api/orders/preview", json={"code": "017641", "amount": 10}).json()
    assert first["ok"]
    client.post("/api/orders", json={"code": "017641", "amount": 10})
    second = client.post("/api/orders/preview", json={"code": "017641", "amount": 1}).json()
    assert not second["ok"]
    assert any("限额" in reason for reason in second["reasons"])
    suspended = client.post("/api/orders/preview", json={"code": "050025", "amount": 10}).json()
    assert not suspended["ok"]
    assert any("暂停" in reason for reason in suspended["reasons"])


def test_cancel_releases_frozen_funds_and_idempotency_prevents_duplicate_order(client: TestClient) -> None:
    client.post("/api/deposit", json={"amount": 3000})
    client.post("/api/settings", json={"platform_012692_verified": True})
    body = {"code": "012692", "amount": 100, "idempotency_key": "fund-order-1"}
    first = client.post("/api/orders", json=body)
    duplicate = client.post("/api/orders", json=body)
    assert first.status_code == duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert len(client.get("/api/state").json()["orders"]) == 1
    cancelled = client.post(f"/api/orders/{first.json()['id']}/cancel", json={})
    assert cancelled.status_code == 200
    account = client.get("/api/state").json()["account"]
    assert account["cash"] == 3000
    assert account["reserved_cash"] == 0


def test_stock_buy_remains_blocked_without_research_approval(client: TestClient) -> None:
    client.post("/api/deposit", json={"amount": 3000})
    set_quote(client, "601398", 8.49)
    r = client.post("/api/orders/preview", json={"code": "601398", "quantity": 100, "limit_price": 8.49})
    assert not r.json()["ok"]
    assert any("研究卡" in reason for reason in r.json()["reasons"])


def test_pending_sell_quantity_is_reserved_and_stamp_rate_cannot_be_blank(client: TestClient) -> None:
    client.post("/api/deposit", json={"amount": 3000})
    client.post("/api/settings", json={"commission_rate": 0, "minimum_commission": 0})
    set_topix(client, 1.844, 1.8493)
    buy = client.post("/api/orders", json={"code": "513800", "quantity": 100, "limit_price": 1.844}).json()
    assert client.post(f"/api/orders/{buy['id']}/fill", json={}).status_code == 200
    first = client.post("/api/orders/preview", json={"code": "513800", "side": "sell", "quantity": 100, "limit_price": 1.844})
    assert first.json()["ok"]
    order = client.post("/api/orders", json={"code": "513800", "side": "sell", "quantity": 100, "limit_price": 1.844}).json()
    second = client.post("/api/orders/preview", json={"code": "513800", "side": "sell", "quantity": 100, "limit_price": 1.844})
    assert not second.json()["ok"]
    assert any("模拟持仓" in reason for reason in second.json()["reasons"])
    assert client.post(f"/api/orders/{order['id']}/cancel", json={}).status_code == 200

    invalid_settings = client.post("/api/settings", json={"stamp_duty_verified": True})
    assert invalid_settings.status_code == 400


def test_deposit_does_not_erase_drawdown(client: TestClient) -> None:
    client.post("/api/deposit", json={"amount": 3000})
    client.post("/api/settings", json={"platform_012692_verified": True, "fee_012692": 0, "commission_rate": 0, "minimum_commission": 0})
    order = client.post("/api/orders", json={"code": "012692", "amount": 1500}).json()
    confirmed = client.post(f"/api/orders/{order['id']}/confirm", json={"nav": 1, "nav_date": "2026-10-27", "evidence": "test"})
    assert confirmed.status_code == 200
    set_quote(client, "012692", 0.4)
    before = client.get("/api/state").json()["account"]["drawdown"]
    assert before <= -0.2
    client.post("/api/deposit", json={"amount": 1000})
    after = client.get("/api/state").json()["account"]["drawdown"]
    assert after <= -0.2


def test_route_preview_keeps_unverified_assets_in_cash_and_routes_bond_to_fifty_percent(client: TestClient) -> None:
    blocked = client.post("/api/route-preview", json={"amount": 3000}).json()
    assert blocked["allocations"] == {"sp500": "0.00", "topix": "0.00", "bond": "0.00"}
    assert blocked["stock_reserve"] == "150.00"
    assert blocked["cash"] == "2850.00"
    assert blocked["total"] == "3000.00"

    client.post("/api/settings", json={"platform_012692_verified": True})
    eligible = client.post("/api/route-preview", json={"amount": 3000}).json()
    assert eligible["allocations"]["bond"] == "1500.00"
    assert eligible["bond_topup_from_unspent_overseas"] == "150.00"
    assert eligible["cash"] == "1350.00"
    assert eligible["total"] == "3000.00"

    client.post("/api/settings", json={"platform_017641_verified": True})
    alternate = client.post("/api/route-preview", json={"amount": 3000}).json()
    assert alternate["allocations"]["sp500"] == "10.00"
    assert alternate["allocations"]["bond"] == "1500.00"
    assert alternate["bond_topup_from_unspent_overseas"] == "150.00"


def test_failed_quote_refresh_keeps_old_seed_and_reports_failure(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import personal_investment_simulator.app as module

    before = next(x for x in client.get("/api/state").json()["quotes"] if x["code"] == "513800")

    def fail(*args, **kwargs):
        raise FileNotFoundError("hithink CLI unavailable in this test")

    monkeypatch.setattr(module.subprocess, "run", fail)
    result = client.post("/api/quotes/refresh", json={}).json()
    after = next(x for x in client.get("/api/state").json()["quotes"] if x["code"] == "513800")
    assert not result["ok"]
    assert before["price"] == after["price"]
    assert before["observed_at"] == after["observed_at"]
