from datetime import date
import pytest
from fastapi.testclient import TestClient

from personal_investment_simulator import backtest
from personal_investment_simulator.app import create_app
from personal_investment_simulator.backtest import LongDcaRequest, _planned_dates, _resolve_schedule_date, calculate_long_dca


def fund_history(rows, events=()):
    return {
        "code": "050025",
        "kind": "fund",
        "rows": rows,
        "events": list(events),
        "source": "synthetic test",
        "request_ids": [],
        "first": rows[0]["date"],
        "last": rows[-1]["date"],
        "price_basis": "unit nav",
    }


def request(**overrides):
    values = {
        "start_date": date(2024, 1, 1),
        "stop_date": date(2024, 2, 1),
        "evaluation_date": date(2024, 2, 2),
        "monthly_amount": 3000,
        "fee_rate": 0,
    }
    values.update(overrides)
    return LongDcaRequest(**values)


def test_long_dca_hand_calculation_with_two_monthly_contributions():
    history = fund_history([
        {"date": "2024-01-02", "open": 1, "close": 1},
        {"date": "2024-02-01", "open": 2, "close": 2},
        {"date": "2024-02-02", "open": 2, "close": 2},
    ])
    result = calculate_long_dca(request(), history)
    assert result["summary"]["invested"] == 6000
    assert result["summary"]["units"] == 4500
    assert result["summary"]["value"] == 9000
    assert result["summary"]["profit"] == 3000
    assert result["summary"]["simple_return"] == pytest.approx(0.5)
    assert result["summary"]["executed_installments"] == 2
    assert [x["date"] for x in result["ledger"] if x["type"] == "定投申购"] == ["2024-01-02", "2024-02-01"]


def test_fee_and_unit_rounding_leave_small_cash_and_reduce_value():
    history = fund_history([
        {"date": "2024-01-02", "open": 1, "close": 1},
        {"date": "2024-01-03", "open": 1, "close": 1},
    ])
    result = calculate_long_dca(request(stop_date=date(2024, 1, 1), evaluation_date=date(2024, 1, 3), fee_rate="0.0012"), history)
    summary = result["summary"]
    assert summary["fees"] == pytest.approx(3.60)
    assert summary["units"] == 2996.4
    assert summary["value"] < summary["invested"]
    assert summary["profit"] == -3.6


def test_dividend_entitlement_and_reinvestment_use_disclosed_dates():
    history = fund_history(
        [
            {"date": "2024-01-02", "open": 1, "close": 1},
            {"date": "2024-01-06", "open": 1, "close": 1},
            {"date": "2024-01-08", "open": 1, "close": 1},
        ],
        [{"registration": "2024-01-04", "date": "2024-01-05", "payment": "2024-01-08", "reinvest_date": "2024-01-06", "cash": 0.1}],
    )
    result = calculate_long_dca(request(stop_date=date(2024, 1, 1), evaluation_date=date(2024, 1, 8), monthly_amount=1000), history)
    summary = result["summary"]
    assert summary["dividends"] == pytest.approx(100)
    assert summary["units"] == 1100
    assert summary["invested"] == 1000
    assert any(x["type"] == "分红再投资" and x["date"] == "2024-01-06" for x in result["ledger"])


def test_fund_share_split_changes_units_without_changing_value():
    history = fund_history([
        {"date": "2024-01-02", "open": 1, "close": 1},
        {"date": "2024-01-04", "open": 0.5, "close": 0.5},
        {"date": "2024-01-05", "open": 0.5, "close": 0.5},
    ])
    history["splits"] = [{"date": "2024-01-04", "factor": "2"}]
    result = calculate_long_dca(request(stop_date=date(2024, 1, 1), evaluation_date=date(2024, 1, 5), monthly_amount=1000), history)
    assert result["summary"]["units"] == 2000
    assert result["summary"]["value"] == 1000
    assert any(x["type"] == "份额拆分" for x in result["ledger"])


def test_akshare_adapter_normalizes_full_nav_and_split_factor(monkeypatch):
    import akshare as ak
    import pandas as pd

    def fake_fund_info_em(symbol, indicator, period):
        if indicator == "单位净值走势":
            return pd.DataFrame({"净值日期": [date(2016, 1, 5), date(2016, 1, 4)], "单位净值": [1.2, 1.1]})
        return pd.DataFrame({"拆分折算日": ["2016-02-01"], "拆分折算比例": ["1:1.5"]})

    monkeypatch.setattr(ak, "fund_open_fund_info_em", fake_fund_info_em)
    nav = backtest._akshare_fund_nav("050025")["data"]["item"]
    splits = backtest._akshare_fund_splits("050025")["data"]["item"]
    assert [backtest._day(row["nav_date"]) for row in nav] == ["2016-01-04", "2016-01-05"]
    assert splits == [{"date": "2016-02-01", "factor": "1.5"}]


def test_xirr_is_unavailable_for_same_day_purchase_and_valuation():
    history = fund_history([{"date": "2024-01-02", "open": 1, "close": 1}])
    result = calculate_long_dca(request(stop_date=date(2024, 1, 1), evaluation_date=date(2024, 1, 2)), history)
    assert result["summary"]["xirr"] is None


@pytest.mark.parametrize(
    ("frequency", "start", "stop", "schedule_day", "weekday", "expected"),
    [
        ("weekly", date(2024, 1, 2), date(2024, 1, 17), "1", 0, ["2024-01-08", "2024-01-15"]),
        ("biweekly", date(2024, 1, 3), date(2024, 1, 31), "1", 2, ["2024-01-03", "2024-01-17", "2024-01-31"]),
        ("monthly", date(2024, 1, 15), date(2024, 3, 31), "10", 0, ["2024-01-15", "2024-02-10", "2024-03-10"]),
        ("quarterly", date(2024, 1, 15), date(2024, 12, 31), "10", 0, ["2024-01-15", "2024-04-10", "2024-07-10", "2024-10-10"]),
        ("semiannual", date(2024, 3, 15), date(2025, 3, 31), "1", 0, ["2024-03-15", "2024-09-01", "2025-03-01"]),
        ("annual", date(2024, 3, 15), date(2026, 3, 31), "last", 0, ["2024-03-31", "2025-03-31", "2026-03-31"]),
    ],
)
def test_recurring_schedule_dates(frequency, start, stop, schedule_day, weekday, expected):
    actual = _planned_dates(start, stop, frequency, schedule_day, weekday)
    assert [day.isoformat() for day in actual] == expected


def test_daily_schedule_uses_only_available_nav_dates():
    actual = _planned_dates(
        date(2024, 1, 2),
        date(2024, 1, 5),
        "daily",
        available_dates=["2024-01-02", "2024-01-03", "2024-01-08"],
    )
    assert [day.isoformat() for day in actual] == ["2024-01-02", "2024-01-03"]


def test_month_end_resolves_to_last_available_date_inside_period():
    actual = _resolve_schedule_date(
        date(2024, 1, 31),
        ["2024-01-30", "2024-02-01"],
        period_start=date(2024, 1, 1),
    )
    assert actual == "2024-01-30"


def test_weekday_long_dca_rolls_forward_and_keeps_planned_date():
    history = fund_history([
        {"date": "2024-01-08", "open": 1, "close": 1},
        {"date": "2024-01-12", "open": 1, "close": 1},
        {"date": "2024-01-15", "open": 1, "close": 1},
    ])
    result = calculate_long_dca(request(
        start_date=date(2024, 1, 1),
        stop_date=date(2024, 1, 12),
        evaluation_date=date(2024, 1, 15),
        monthly_amount=1000,
        frequency="weekly",
        schedule_weekday=4,
        fee_rate=0,
    ), history)
    purchases = [row for row in result["ledger"] if row["type"] == "定投申购"]
    assert [(row["planned_date"], row["date"]) for row in purchases] == [
        ("2024-01-05", "2024-01-08"),
        ("2024-01-12", "2024-01-12"),
    ]
    assert result["summary"]["executed_installments"] == 2


def test_daily_long_dca_executes_once_per_available_nav_date():
    history = fund_history([
        {"date": "2024-01-02", "open": 1, "close": 1},
        {"date": "2024-01-03", "open": 1, "close": 1},
        {"date": "2024-01-05", "open": 1, "close": 1},
        {"date": "2024-01-08", "open": 1, "close": 1},
    ])
    result = calculate_long_dca(request(
        start_date=date(2024, 1, 2),
        stop_date=date(2024, 1, 5),
        evaluation_date=date(2024, 1, 8),
        monthly_amount=1000,
        frequency="daily",
        fee_rate=0,
    ), history)
    assert result["summary"]["planned_installments"] == 3
    assert result["summary"]["executed_installments"] == 3
    assert result["summary"]["invested"] == 3000
    assert result["summary"]["actual_end"] == "2024-01-08"


def test_quarterly_month_end_backtest_stops_contributions_but_keeps_valuing():
    history = fund_history([
        {"date": "2024-01-02", "open": 1, "close": 1},
        {"date": "2024-01-31", "open": 1, "close": 1},
        {"date": "2024-04-30", "open": 1, "close": 1},
        {"date": "2024-07-31", "open": 1, "close": 1},
        {"date": "2024-10-31", "open": 1, "close": 1},
        {"date": "2024-11-01", "open": 1, "close": 1},
    ])
    result = calculate_long_dca(request(
        start_date=date(2024, 1, 1),
        stop_date=date(2024, 10, 31),
        evaluation_date=date(2024, 11, 1),
        monthly_amount=1000,
        frequency="quarterly",
        schedule_day="last",
        fee_rate=0,
    ), history)
    assert result["summary"]["planned_installments"] == 4
    assert result["summary"]["executed_installments"] == 4
    assert result["summary"]["invested"] == 4000
    assert result["summary"]["actual_end"] == "2024-11-01"
    assert result["summary"]["frequency"] == "quarterly"


def test_month_end_in_leap_year_and_stop_boundary():
    assert [day.isoformat() for day in _planned_dates(
        date(2024, 1, 1), date(2024, 3, 31), "monthly", "last"
    )] == ["2024-01-31", "2024-02-29", "2024-03-31"]
    assert [day.isoformat() for day in _planned_dates(
        date(2024, 1, 1), date(2024, 2, 28), "monthly", "1"
    )] == ["2024-01-01", "2024-02-01"]


def test_missing_2016_coverage_is_rejected_instead_of_shortened():
    history = fund_history([
        {"date": "2020-01-02", "open": 1, "close": 1},
        {"date": "2020-01-03", "open": 1, "close": 1},
    ])
    with pytest.raises(ValueError, match="未覆盖开始日"):
        calculate_long_dca(
            request(start_date=date(2016, 1, 1), stop_date=date(2020, 1, 1), evaluation_date=date(2020, 1, 3)),
            history,
        )


def test_long_dca_api_records_run_and_exports_csv_without_touching_paper_account(tmp_path, monkeypatch):
    monkeypatch.setattr(backtest, "load_history", lambda code, start, end, refresh=False: fund_history([
        {"date": "2016-01-04", "open": 1, "close": 1},
        {"date": "2016-01-05", "open": 1, "close": 1},
    ]))
    client = TestClient(create_app(tmp_path / "paper.sqlite3", seed=False))
    response = client.post("/api/backtests/2016-dca", json={
        "start_date": "2016-01-01",
        "stop_date": "2016-01-01",
        "evaluation_date": "2016-01-05",
        "monthly_amount": 3000,
        "frequency": "monthly",
        "schedule_day": "1",
        "schedule_weekday": 0,
        "fee_rate": 0.0012,
    })
    assert response.status_code == 200
    run_id = response.json()["id"]
    result = client.get(f"/api/backtests/{run_id}").json()
    assert result["status"] == "done"
    assert result["result"]["summary"]["invested"] == 3000
    assert result["result"]["summary"]["frequency"] == "monthly"
    assert result["result"]["summary"]["schedule_day"] == "1"
    export = client.get(f"/api/backtests/{run_id}/ledger.csv")
    assert export.status_code == 200
    assert export.content.startswith(b"\xef\xbb\xbf")
    assert "定投申购" in export.content.decode("utf-8-sig")
    assert client.get("/api/state").json()["account"]["nav"] == 0
