from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
import math

import pytest
from fastapi.testclient import TestClient

from personal_investment_simulator import policy_research as pr
from personal_investment_simulator import stock_research as sr
from personal_investment_simulator.app import create_app


EVENT_ID = "CN_GRID_2024"
CODE = "600406"


def reviewed_link(**overrides):
    item = {
        "event_id": EVENT_ID,
        "code": CODE,
        "strength": 1.0,
        "channel": "资本开支",
        "business_evidence": "公司参与电网设备项目，政策规划可能影响订单和交付节奏。",
        "evidence_url": "https://example.com/company-evidence",
        "evidence_available_at": "2024-08-02",
        "reviewed": True,
    }
    item.update(overrides)
    return item


def weekdays(start: date, end: date) -> list[date]:
    days = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def test_policy_catalog_is_loaded_without_inventing_company_links(tmp_path: Path):
    overview = pr.get_overview(tmp_path / "policy.sqlite3")

    assert overview["summary"]["event_count"] == 9
    assert overview["summary"]["company_count"] == 65
    assert overview["summary"]["link_count"] == 0
    assert overview["summary"]["source_documents_archived"] == 0


def test_reviewed_company_link_requires_prior_traceable_evidence(tmp_path: Path):
    path = tmp_path / "policy.sqlite3"
    pr.initialize(path)

    with pytest.raises(ValueError, match="证据链接"):
        pr.upsert_link(reviewed_link(evidence_url=""), path)
    with pytest.raises(ValueError, match="不能晚于"):
        pr.upsert_link(reviewed_link(evidence_available_at="2024-08-03"), path)

    saved = pr.upsert_link(reviewed_link(), path)
    assert saved["reviewed"] is True
    assert pr.get_overview(path)["summary"]["reviewed_link_count"] == 1


def test_study_hash_covers_the_evaluation_cutoff(tmp_path: Path):
    path = tmp_path / "policy.sqlite3"
    pr.initialize(path)
    pr.upsert_link(reviewed_link(), path)

    first = pr.get_study(pr.create_study({"evaluation_date": "2024-08-16"}, path), path)
    second = pr.get_study(pr.create_study({"evaluation_date": "2024-08-19"}, path), path)

    assert first["inputs"]["input_hash"] != second["inputs"]["input_hash"]


def test_policy_exposure_distinguishes_missing_evidence_from_known_zero():
    event = next(item for item in pr.load_events() if item["event_id"] == EVENT_ID)
    companies = [
        {"code": CODE, "name": "国电南瑞", "sector": "电网设备", "role": "primary"},
        {"code": "601877", "name": "正泰电器", "sector": "电网设备", "role": "primary"},
    ]
    link = reviewed_link()
    sessions = [day.isoformat() for day in weekdays(date(2024, 8, 1), date(2024, 12, 31))]
    current = pr._policy_exposures([event], [link], companies, sessions, "2024-08-16")
    expired = pr._policy_exposures([event], [link], companies, sessions, "2024-12-31")

    assert current[0]["status"] == "available"
    assert current[0]["score"] > 0
    assert current[1]["status"] == "missing_evidence"
    assert current[1]["score"] is None
    assert expired[0]["status"] == "available"
    assert expired[0]["score"] == 0


def test_policy_exposure_does_not_use_events_after_evaluation_date():
    event = next(item for item in pr.load_events() if item["event_id"] == EVENT_ID)
    company = {"code": CODE, "name": "国电南瑞", "sector": "电网设备", "role": "primary"}
    sessions = [day.isoformat() for day in weekdays(date(2024, 1, 1), date(2024, 7, 31))]

    result = pr._policy_exposures([event], [reviewed_link()], [company], sessions, "2024-07-31")

    assert result[0]["status"] == "missing_evidence"
    assert result[0]["score"] is None


def test_market_model_estimates_event_window_from_synthetic_returns():
    sessions = [day.isoformat() for day in weekdays(date(2023, 1, 2), date(2024, 6, 30))]
    event_index = 300
    market_returns: list[float | None] = [None]
    stock_returns: list[float | None] = [None]
    for index in range(1, len(sessions)):
        market = 0.001 * math.sin(index * 0.17) + 0.0003 * math.cos(index * 0.037)
        residual = 0.0002 * math.sin(index * 0.71)
        market_returns.append(market)
        stock_returns.append(0.0001 + 1.15 * market + residual + (0.004 if event_index <= index <= event_index + 5 else 0))
    event = {"event_id": EVENT_ID, "family": "CN_DISTRIBUTION_GRID", "published_at": sessions[event_index], "date_only": False}

    row = pr._estimate(event, reviewed_link(), event_index, sessions, stock_returns, market_returns, (0, 5))

    assert row["status"] == "exploratory"
    assert row["n_estimation"] >= pr.MIN_ESTIMATION_OBSERVATIONS
    assert row["car"] > 0
    assert row["actual_return"] > 0
    assert 0 <= row["p_value"] <= 1
    assert row["ci_low"] < row["ci_high"]


def test_hithink_history_adapter_normalizes_stock_index_and_beijing_bars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sr, "RAW_DIR", tmp_path / "raw")
    calls = []
    dates = [date(2024, 1, day) for day in range(2, 6)]

    def fake_hithink(_bt, *args):
        calls.append(args)
        return {
            "meta": {"request_id": f"request-{len(calls)}"},
            "data": {"item": [
                {"date_ms": sr._ms(day), "open_price": 10 + i, "close_price": 10.5 + i}
                for i, day in enumerate(dates)
            ]},
        }

    monkeypatch.setattr(sr, "_hithink", fake_hithink)
    start, end = dates[0], dates[-1]

    stock = sr.load_or_fetch_daily_bars("600406.SH", start, end)
    cached = sr.load_or_fetch_daily_bars("600406.SH", start, end)
    index = sr.load_or_fetch_daily_bars("000300.SH", start, end, instrument_type="index")
    beijing = sr.load_or_fetch_daily_bars("830799.BJ", start, end)

    assert stock["prices"][0] == {"date": "2024-01-02", "open": 10.0, "close": 10.5}
    assert stock["request_ids"] == ["request-1"]
    assert cached["prices"] == stock["prices"]
    assert len(calls) == 3
    assert calls[0][0:2] == ("market", "history")
    assert calls[0][calls[0].index("--start-ms") + 1] == str(sr._ms(date(2024, 1, 1)))
    assert calls[0][calls[0].index("--end-ms") + 1] == str(sr._ms(date(2024, 12, 31)))
    assert calls[1][0:2] == ("index", "history")
    assert calls[2][calls[2].index("--thscode") + 1] == "830799.BJ"
    assert beijing["thscode"] == "830799.BJ"


def test_policy_api_runs_with_historical_data_and_keeps_paper_account_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    evaluation_date = date.today()
    sessions = weekdays(date(2022, 1, 3), evaluation_date - timedelta(days=1))
    market_prices: list[dict[str, float | str]] = []
    stock_prices: list[dict[str, float | str]] = []
    market_close = 100.0
    stock_close = 50.0
    for index, day in enumerate(sessions):
        if index:
            market_return = 0.001 * math.sin(index * 0.13) + 0.0002 * math.cos(index * 0.031)
            stock_return = 0.0001 + 1.1 * market_return + 0.00015 * math.sin(index * 0.61)
            market_close *= 1 + market_return
            stock_close *= 1 + stock_return
        market_prices.append({"date": day.isoformat(), "open": market_close, "close": market_close})
        stock_prices.append({"date": day.isoformat(), "open": stock_close, "close": stock_close})

    def fake_history(thscode, start, end, *, instrument_type="stock", refresh=False):
        source = market_prices if instrument_type == "index" else stock_prices
        return {
            "thscode": thscode,
            "instrument_type": instrument_type,
            "prices": [row for row in source if start.isoformat() <= row["date"] <= end.isoformat()],
            "request_ids": [f"synthetic-{thscode}"],
        }

    monkeypatch.setattr(sr, "load_or_fetch_daily_bars", fake_history)
    client = TestClient(create_app(tmp_path / "simulator.sqlite3", seed=True))

    page = client.get("/policy")
    assert page.status_code == 200
    assert 'id="linkForm"' in page.text
    assert 'id="runStudy"' in page.text
    overview = client.get("/api/policy/overview").json()
    assert overview["summary"]["event_count"] == 9
    assert overview["summary"]["company_count"] == 65
    assert overview["summary"]["link_count"] == 0

    deposit = client.post("/api/deposit", json={"amount": 1000, "note": "policy isolation"})
    assert deposit.status_code == 200
    saved = client.put(f"/api/policy/links/{EVENT_ID}/{CODE}", json={key: value for key, value in reviewed_link().items() if key not in {"event_id", "code"}})
    assert saved.status_code == 200, saved.text

    started = client.post("/api/policy/studies", json={
        "event_ids": [EVENT_ID],
        "codes": [CODE],
        "evaluation_date": evaluation_date.isoformat(),
        "refresh_data": False,
    })
    assert started.status_code == 200, started.text
    result = client.get(f"/api/policy/studies/{started.json()['id']}").json()

    assert result["status"] == "done"
    assert result["result"]["successful_study_count"] > 0
    assert result["result"]["benchmark"]["request_ids"] == ["synthetic-000300.SH"]
    assert result["result"]["predictive_layer"]["status"] == "blocked"
    assert client.get("/api/state").json()["account"]["nav"] == 1000
    assert client.get("/api/state").json()["orders"] == []


def test_historical_cutoff_excludes_later_policy_events_without_fetching_prices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "policy.sqlite3"
    pr.initialize(path)
    pr.upsert_link(reviewed_link(), path)
    study_id = pr.create_study({"evaluation_date": "2024-07-31"}, path)

    def unexpected_fetch(*args, **kwargs):
        raise AssertionError("prices must not be fetched when all policy events are in the future")

    monkeypatch.setattr(sr, "load_or_fetch_daily_bars", unexpected_fetch)
    pr.execute_study(study_id, path)
    result = pr.get_study(study_id, path)

    assert result["status"] == "blocked_missing_links"
    assert result["result"]["exclusions"][0]["reason"] == "政策事件晚于研究截止日"
