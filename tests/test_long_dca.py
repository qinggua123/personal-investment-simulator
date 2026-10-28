from datetime import date
import pytest
from fastapi.testclient import TestClient

from personal_investment_simulator import backtest
from personal_investment_simulator.app import create_app
from personal_investment_simulator.backtest import LongDcaRequest, calculate_long_dca


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
        "fee_rate": 0.0012,
    })
    assert response.status_code == 200
    run_id = response.json()["id"]
    result = client.get(f"/api/backtests/{run_id}").json()
    assert result["status"] == "done"
    assert result["result"]["summary"]["invested"] == 3000
    export = client.get(f"/api/backtests/{run_id}/ledger.csv")
    assert export.status_code == 200
    assert export.content.startswith(b"\xef\xbb\xbf")
    assert "定投申购" in export.content.decode("utf-8-sig")
    assert client.get("/api/state").json()["account"]["nav"] == 0
