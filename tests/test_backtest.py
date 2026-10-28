from datetime import date

from fastapi.testclient import TestClient

from personal_investment_simulator.app import create_app
from personal_investment_simulator import backtest as bt
from personal_investment_simulator.backtest import BacktestRequest, _day, calculate


def history(code, kind, rows, events=()):
    return {"code": code, "kind": kind, "rows": rows, "events": list(events), "source": "synthetic test", "first": rows[0]["date"], "last": rows[-1]["date"], "price_basis": "test"}


def test_stock_lot_fee_and_unspent_cash():
    h = history("600690", "stock", [{"date": "2025-01-02", "open": 10, "close": 10}, {"date": "2025-01-03", "open": 11, "close": 11}])
    req = BacktestRequest(strategy="once", start_date=date(2025, 1, 2), stop_date=date(2025, 1, 2), evaluation_date=date(2025, 1, 3), allocations=[{"code": "600690", "amount": 1500}])
    result = calculate(req, {"600690": h})
    assert result["positions"][0]["units"] == 100
    assert result["summary"]["fees"] == 5
    assert result["summary"]["cash"] == 495
    assert result["summary"]["value"] == 1595


def test_fund_fee_and_dividend_reinvestment_after_payment():
    rows = [{"date": f"2025-01-0{i}", "open": 1, "close": 1} for i in range(2, 7)]
    h = history("012692", "fund", rows, [{"date": "2025-01-04", "registration": "2025-01-03", "payment": "2025-01-05", "cash": 0.1, "bonus": 0}])
    req = BacktestRequest(strategy="once", start_date=date(2025, 1, 2), stop_date=date(2025, 1, 2), evaluation_date=date(2025, 1, 6), allocations=[{"code": "012692", "amount": 1000}], bond_fee_rate=0, reinvest_dividends=True)
    result = calculate(req, {"012692": h})
    assert result["positions"][0]["units"] == 1100
    assert result["summary"]["dividends"] == 100
    assert any(x["date"] == "2025-01-05" and x["type"] == "分红" for x in result["ledger"])


def test_backtest_pages_and_account_are_independent(tmp_path):
    client = TestClient(create_app(tmp_path / "paper.sqlite3", seed=False))
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="dcaFrequency"' in page.text
    assert 'id="scheduleDay"' in page.text
    assert 'id="scheduleWeekday"' in page.text
    assert client.get("/trade").status_code == 200
    assert client.get("/settings").status_code == 200
    assert len(client.get("/api/backtest/instruments").json()["items"]) == 9
    assert client.get("/api/state").json()["account"]["nav"] == 0
    assert client.get("/api/backtests").json()["items"] == []


def test_custom_stock_lookup_registers_backtest_only_and_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, "CUSTOM_STOCKS_PATH", tmp_path / "backtest_stocks.json")
    calls = []

    def fake_cli(*args):
        calls.append(args)
        return {"data": {"item": [{"ticker": "601318", "name": "中国平安", "exchange": "SH", "thscode": "601318.SH", "asset_type": "a-share"}]}}

    monkeypatch.setattr(bt, "_cli", fake_cli)
    client = TestClient(create_app(tmp_path / "paper.sqlite3", seed=False))
    response = client.post("/api/backtest/instruments", json={"code": "601318"})
    assert response.status_code == 200
    assert response.json()["item"] == {"code": "601318", "name": "中国平安", "kind": "stock", "bucket": "stock", "custom": True, "exchange": "SH"}
    assert calls == [("symbol", "search", "--q", "601318", "--asset-type", "a-share", "--limit", "50")]

    listed = client.get("/api/backtest/instruments").json()["items"]
    assert any(x["code"] == "601318" and x["custom"] for x in listed)
    assert "601318" not in client.get("/api/state").json()["instruments"]
    assert bt._suffix("601318") == "601318.SH"
    request = BacktestRequest(strategy="once", start_date=date(2025, 1, 2), stop_date=date(2025, 1, 2), evaluation_date=date(2025, 1, 3), allocations=[{"code": "601318", "amount": 3000}])
    assert request.allocations[0].code == "601318"

    reopened = TestClient(create_app(tmp_path / "paper.sqlite3", seed=False))
    assert any(x["code"] == "601318" for x in reopened.get("/api/backtest/instruments").json()["items"])


def test_custom_stock_lookup_rejects_nonmatching_and_ambiguous_symbols(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, "CUSTOM_STOCKS_PATH", tmp_path / "backtest_stocks.json")
    client = TestClient(create_app(tmp_path / "paper.sqlite3", seed=False))
    invalid = client.post("/api/backtest/instruments", json={"code": "not-a-code"})
    assert invalid.status_code == 422

    monkeypatch.setattr(bt, "_cli", lambda *args: {"data": {"item": []}})
    missing = client.post("/api/backtest/instruments", json={"code": "999999"})
    assert missing.status_code == 404

    monkeypatch.setattr(bt, "_cli", lambda *args: {"data": {"item": [
        {"ticker": "123456", "name": "示例沪股", "exchange": "SH", "thscode": "123456.SH", "asset_type": "a-share"},
        {"ticker": "123456", "name": "示例深股", "exchange": "SZ", "thscode": "123456.SZ", "asset_type": "a-share"},
    ]}})
    ambiguous = client.post("/api/backtest/instruments", json={"code": "123456"})
    assert ambiguous.status_code == 400
    disambiguated = client.post("/api/backtest/instruments", json={"code": "123456.SZ"})
    assert disambiguated.status_code == 200
    assert disambiguated.json()["item"]["exchange"] == "SZ"


def test_custom_stock_history_fetches_requested_year_and_matching_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, "CUSTOM_STOCKS_PATH", tmp_path / "backtest_stocks.json")
    monkeypatch.setattr(bt, "HISTORY_DIR", tmp_path / "history")
    calls = []

    def fake_cli(*args):
        calls.append(args)
        if args[0:2] == ("symbol", "search"):
            return {"data": {"item": [{"ticker": "601318", "name": "中国平安", "exchange": "SH", "thscode": "601318.SH", "asset_type": "a-share"}]}}
        if args[0:2] == ("market", "history"):
            return {"data": {"item": [
                {"date_ms": bt._ms(date(2014, 1, 2)), "open_price": 10, "close_price": 10.2},
                {"date_ms": bt._ms(date(2014, 1, 3)), "open_price": 10.2, "close_price": 10.3},
            ]}}
        if args[0:2] == ("market", "corporate-actions"):
            return {"data": {"item": []}}
        raise AssertionError(args)

    monkeypatch.setattr(bt, "_cli", fake_cli)
    bt.add_backtest_stock("601318.SH")
    history = bt.load_history("601318", date(2014, 1, 2), date(2014, 12, 31), refresh=True)
    action_call = next(x for x in calls if x[0:2] == ("market", "corporate-actions"))
    assert "--from-date" in action_call
    assert action_call[action_call.index("--from-date") + 1] == "2014-01-01"
    assert history["rows"][0]["date"] == "2014-01-02"
    assert history["source"] == "hithink-finance remote"


def test_market_timestamp_uses_china_calendar_day():
    assert _day(1735747200000) == "2025-01-02"  # 2025-01-01 16:00 UTC


def test_result_ends_at_latest_common_completed_day():
    a = history("513800", "etf", [{"date": "2025-01-02", "open": 1, "close": 1}, {"date": "2025-01-03", "open": 1, "close": 1}])
    b = history("012692", "fund", [{"date": "2025-01-02", "open": 1, "close": 1}, {"date": "2025-01-03", "open": 1, "close": 1}, {"date": "2025-01-06", "open": 2, "close": 2}])
    req = BacktestRequest(strategy="once", start_date=date(2025, 1, 2), stop_date=date(2025, 1, 2), evaluation_date=date(2025, 1, 6), allocations=[{"code": "513800", "amount": 200}, {"code": "012692", "amount": 200}])
    result = calculate(req, {"513800": a, "012692": b})
    assert result["summary"]["actual_end"] == "2025-01-03"
    assert "2025-01-03" in result["warnings"][0]


def test_non_nav_day_dividend_reinvests_on_next_nav_day_without_new_contribution():
    rows = [{"date": d, "open": 1, "close": 1} for d in ("2025-01-02", "2025-01-03", "2025-01-06")]
    h = history("012692", "fund", rows, [{"date": "2025-01-03", "registration": "2025-01-02", "payment": "2025-01-04", "cash": 0.1, "bonus": 0}])
    req = BacktestRequest(strategy="once", start_date=date(2025, 1, 2), stop_date=date(2025, 1, 2), evaluation_date=date(2025, 1, 6), allocations=[{"code": "012692", "amount": 1000}], bond_fee_rate=0)
    result = calculate(req, {"012692": h})
    assert result["summary"]["invested"] == 1000
    assert result["positions"][0]["units"] == 1100
    assert any(x["date"] == "2025-01-06" and x["type"] == "买入" for x in result["ledger"])


def test_monthly_stock_budget_accumulates_until_one_lot_is_affordable():
    rows = [{"date": d, "open": 25, "close": 25} for d in ("2025-01-02", "2025-02-03", "2025-03-03")]
    h = history("600690", "stock", rows)
    req = BacktestRequest(strategy="monthly", start_date=date(2025, 1, 2), stop_date=date(2025, 3, 3), evaluation_date=date(2025, 3, 3), allocations=[{"code": "600690", "amount": 1000}])
    result = calculate(req, {"600690": h})
    assert result["summary"]["invested"] == 3000
    assert result["summary"]["planned_entries"] == 3
    assert result["summary"]["executed_entries"] == 3
    assert result["positions"][0]["units"] == 100
    assert result["summary"]["cash"] == 495


def test_month_end_schedule_uses_last_available_day_in_each_month():
    rows = [
        {"date": "2024-01-02", "open": 1, "close": 1},
        {"date": "2024-01-30", "open": 1, "close": 1},
        {"date": "2024-02-28", "open": 1, "close": 1},
        {"date": "2024-03-01", "open": 1, "close": 1},
    ]
    h = history("012692", "fund", rows)
    req = BacktestRequest(
        strategy="monthly",
        start_date=date(2024, 1, 2),
        stop_date=date(2024, 2, 29),
        evaluation_date=date(2024, 3, 1),
        schedule_day="last",
        allocations=[{"code": "012692", "amount": 1000}],
        bond_fee_rate=0,
    )
    result = calculate(req, {"012692": h})
    purchases = [row for row in result["ledger"] if row["type"] == "买入" and row.get("planned_date")]
    assert [(row["planned_date"], row["date"]) for row in purchases] == [
        ("2024-01-31", "2024-01-30"),
        ("2024-02-29", "2024-02-28"),
    ]
    assert result["summary"]["planned_entries"] == 2
    assert result["summary"]["executed_entries"] == 2


def test_daily_multi_asset_backtest_uses_actual_asset_dates_once_each():
    rows = [
        {"date": "2025-01-02", "open": 1, "close": 1},
        {"date": "2025-01-03", "open": 1, "close": 1},
        {"date": "2025-01-06", "open": 1, "close": 1},
    ]
    h = history("012692", "fund", rows)
    req = BacktestRequest(
        strategy="daily",
        start_date=date(2025, 1, 2),
        stop_date=date(2025, 1, 6),
        evaluation_date=date(2025, 1, 6),
        allocations=[{"code": "012692", "amount": 1000}],
        bond_fee_rate=0,
    )
    result = calculate(req, {"012692": h})
    assert result["summary"]["planned_entries"] == 3
    assert result["summary"]["executed_entries"] == 3
    assert result["summary"]["invested"] == 3000
