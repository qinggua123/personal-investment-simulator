"""Explainable point-in-time stock research and portfolio backtests.

This module is deliberately isolated from the simulator's paper-account ledger.
All external data is cached under runtime/research and each run stores its own
inputs, status, provenance and result in a separate SQLite database.
"""
from __future__ import annotations

import bisect
import calendar
import csv
from contextlib import closing
from datetime import date, datetime, timedelta
import io
import json
import math
import re
import sqlite3
import statistics
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / "runtime" / "research"
RAW_DIR = RUNTIME / "raw"
RUN_DB = RUNTIME / "research.sqlite3"
EXPORT_DIR = RUNTIME / "exports"
CN_TZ = ZoneInfo("Asia/Shanghai")
DB_LOCK = threading.RLock()
MODEL_VERSION = "factor-duPont-v1.1"
CSI300_CODE = "000300.SH"

# Research membership is intentionally separate from app.INSTRUMENTS and the
# paper account's instrument approval flags.
UNIVERSE: list[dict[str, str]] = [
    {"code": "000333", "thscode": "000333.SZ", "name": "美的集团", "sector": "家电/工业技术", "model": "industrial"},
    {"code": "002001", "thscode": "002001.SZ", "name": "新和成", "sector": "化工/医药中间体", "model": "industrial"},
    {"code": "002690", "thscode": "002690.SZ", "name": "美亚光电", "sector": "医疗/检测设备", "model": "industrial"},
    {"code": "600660", "thscode": "600660.SH", "name": "福耀玻璃", "sector": "汽车零部件", "model": "industrial"},
    {"code": "600690", "thscode": "600690.SH", "name": "海尔智家", "sector": "家电", "model": "industrial"},
    {"code": "600900", "thscode": "600900.SH", "name": "长江电力", "sector": "公用事业/水电", "model": "utility"},
    {"code": "600941", "thscode": "600941.SH", "name": "中国移动", "sector": "通信运营", "model": "telecom"},
    {"code": "600036", "thscode": "600036.SH", "name": "招商银行", "sector": "银行", "model": "bank"},
    {"code": "601288", "thscode": "601288.SH", "name": "农业银行", "sector": "银行", "model": "bank"},
    {"code": "601398", "thscode": "601398.SH", "name": "工商银行", "sector": "银行", "model": "bank"},
    {"code": "601939", "thscode": "601939.SH", "name": "建设银行", "sector": "银行", "model": "bank"},
    {"code": "601988", "thscode": "601988.SH", "name": "中国银行", "sector": "银行", "model": "bank"},
    {"code": "601318", "thscode": "601318.SH", "name": "中国平安", "sector": "保险", "model": "insurer"},
    {"code": "600023", "thscode": "600023.SH", "name": "浙能电力", "sector": "公用事业/电力", "model": "utility"},
    {"code": "600377", "thscode": "600377.SH", "name": "宁沪高速", "sector": "交通基础设施", "model": "infrastructure"},
    {"code": "601866", "thscode": "601866.SH", "name": "中远海发", "sector": "航运/金融租赁", "model": "hybrid"},
    {"code": "600406", "thscode": "600406.SH", "name": "国电南瑞", "sector": "电网设备", "model": "industrial"},
    {"code": "002028", "thscode": "002028.SZ", "name": "思源电气", "sector": "电力设备", "model": "industrial"},
    {"code": "002837", "thscode": "002837.SZ", "name": "英维克", "sector": "温控设备", "model": "industrial"},
    {"code": "601869", "thscode": "601869.SH", "name": "长飞光纤", "sector": "光纤网络", "model": "industrial"},
    {"code": "300308", "thscode": "300308.SZ", "name": "中际旭创", "sector": "光通信/光模块", "model": "industrial"},
    {"code": "600845", "thscode": "600845.SH", "name": "宝信软件", "sector": "工业软件/IDC", "model": "industrial"},
    {"code": "300383", "thscode": "300383.SZ", "name": "光环新网", "sector": "IDC/云服务", "model": "industrial"},
    {"code": "688111", "thscode": "688111.SH", "name": "金山办公", "sector": "软件", "model": "industrial"},
    {"code": "688777", "thscode": "688777.SH", "name": "中控技术", "sector": "工业自动化", "model": "industrial"},
]
BY_CODE = {item["code"]: item for item in UNIVERSE}


def today_cn() -> date:
    return datetime.now(CN_TZ).date()


def default_start_date() -> date:
    current = today_cn()
    try:
        return current.replace(year=current.year - 10)
    except ValueError:
        return current.replace(year=current.year - 10, day=28)


def _ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=CN_TZ).timestamp() * 1000)


def _date(ms: int | float | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(float(ms) / 1000, CN_TZ).date().isoformat()


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _dump(value: Any) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class ResearchRunRequest(BaseModel):
    start_date: date = Field(default_factory=default_start_date)
    evaluation_date: date = Field(default_factory=today_cn)
    investment_mode: Literal["fixed", "monthly", "both"] = "both"
    initial_capital: float = Field(default=100_000, ge=0, le=100_000_000)
    monthly_contribution: float = Field(default=3_000, ge=0, le=1_000_000)
    top_n: int = Field(default=8, ge=3, le=25)
    refresh_data: bool = False
    include_ml: bool = True
    commission_rate: float = Field(default=0.0003, ge=0, le=0.02)
    minimum_commission: float = Field(default=5, ge=0, le=100)
    transfer_rate: float = Field(default=0, ge=0, le=0.005)
    slippage_rate: float = Field(default=0.0005, ge=0, le=0.05)

    @model_validator(mode="after")
    def validate_window(self):
        if self.start_date >= self.evaluation_date:
            raise ValueError("开始日期必须早于评估日期")
        if self.evaluation_date > today_cn():
            raise ValueError("评估日期不能晚于今天")
        if (self.evaluation_date - self.start_date).days > 366 * 10:
            raise ValueError("研究区间最长10年，超过数据源历史覆盖范围")
        if self.investment_mode in {"fixed", "both"} and self.initial_capital <= 0:
            raise ValueError("固定资金模式需要正的初始资金")
        if self.investment_mode in {"monthly", "both"} and self.monthly_contribution <= 0:
            raise ValueError("定投模式需要正的月投入金额")
        return self


def initialize(path: Path = RUN_DB) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS research_runs (
            id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            status TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '', request_json TEXT NOT NULL,
            result_json TEXT, error TEXT)""")
        conn.commit()


def create_run(request: ResearchRunRequest, path: Path = RUN_DB) -> str:
    initialize(path)
    run_id = uuid.uuid4().hex
    now = datetime.now(CN_TZ).isoformat(timespec="seconds")
    with DB_LOCK, closing(sqlite3.connect(path)) as conn:
        conn.execute("INSERT INTO research_runs(id,created_at,updated_at,status,progress,message,request_json) VALUES(?,?,?,?,?,?,?)",
                     (run_id, now, now, "queued", 0, "排队中", request.model_dump_json()))
        conn.commit()
    return run_id


def get_run(run_id: str, path: Path = RUN_DB) -> dict[str, Any]:
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(run_id)
    item = dict(row)
    item["request"] = json.loads(item.pop("request_json"))
    item["result"] = json.loads(item["result_json"]) if item.get("result_json") else None
    item.pop("result_json", None)
    return item


def list_runs(path: Path = RUN_DB, limit: int = 30) -> list[dict[str, Any]]:
    initialize(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id,created_at,updated_at,status,progress,message,error FROM research_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def _set_run(run_id: str, path: Path, *, status: str | None = None, progress: float | None = None,
             message: str | None = None, result: dict[str, Any] | None = None, error: str | None = None) -> None:
    assignments, values = ["updated_at=?"], [datetime.now(CN_TZ).isoformat(timespec="seconds")]
    if status is not None:
        assignments.append("status=?"); values.append(status)
    if progress is not None:
        assignments.append("progress=?"); values.append(float(progress))
    if message is not None:
        assignments.append("message=?"); values.append(message)
    if result is not None:
        assignments.append("result_json=?"); values.append(_dump(result))
    if error is not None:
        assignments.append("error=?"); values.append(error[:2000])
    values.append(run_id)
    with DB_LOCK, closing(sqlite3.connect(path)) as conn:
        conn.execute(f"UPDATE research_runs SET {','.join(assignments)} WHERE id=?", values)
        conn.commit()


def _raw_path(name: str) -> Path:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    return RAW_DIR / name


def _fetch_cached(name: str, fetch, refresh: bool, *, refresh_daily: bool = False) -> dict[str, Any]:
    path = _raw_path(name)
    fresh_today = path.exists() and datetime.fromtimestamp(path.stat().st_mtime, CN_TZ).date() == today_cn()
    if path.exists() and not refresh and (not refresh_daily or fresh_today):
        return json.loads(path.read_text(encoding="utf-8"))
    try:
        from . import backtest as bt
    except ImportError:
        import backtest as bt
    # The existing Hithink adapter serializes CLI access and checks API errors.
    payload = fetch(bt)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(_dump(payload), encoding="utf-8")
    tmp.replace(path)
    return payload


def _hithink(bt, *args: str) -> dict[str, Any]:
    with bt.DATA_LOCK:
        return bt._cli(*args)


def _pull_symbol(item: dict[str, str], start: date, end: date, refresh: bool,
                 progress=None, index: int = 0, total: int = 1) -> dict[str, Any]:
    thscode = item["thscode"]
    start_ms, end_ms = _ms(start), _ms(end)
    financial: dict[str, list[dict[str, Any]]] = {}
    requests: list[str] = []
    for label, command in (("income", "income"), ("balance", "balance-sheet"), ("cashflow", "cash-flow")):
        if progress:
            progress(f"财报 {item['name']}：{label}", 8 + 45 * ((index - 1) / max(total, 1)))
        def fetch_statement(bt, command=command):
            return _hithink(bt, "financials", command, "--thscode", thscode, "--period", "quarterly",
                            "--start-ms", str(start_ms), "--end-ms", str(end_ms))
        payload = _fetch_cached(f"{item['code']}_{label}.json", fetch_statement, refresh, refresh_daily=True)
        financial[label] = [row for row in payload.get("data", {}).get("item", [])
                            if row.get("report_date_ms") and 0 < int(row["report_date_ms"]) <= end_ms]
        requests.append(str(payload.get("meta", {}).get("request_id", "")))

    price_rows: list[dict[str, Any]] = []
    for year in range(start.year, end.year + 1):
        first = max(start, date(year, 1, 1))
        last = min(end, date(year, 12, 31))
        def fetch_prices(bt, first=first, last=last):
            return _hithink(bt, "market", "history", "--thscode", thscode, "--start-ms", str(_ms(first)),
                            "--end-ms", str(_ms(last)), "--adjust", "none")
        payload = _fetch_cached(f"{item['code']}_prices_{year}.json", fetch_prices, refresh,
                                refresh_daily=year == today_cn().year)
        price_rows.extend(row for row in payload.get("data", {}).get("item", [])
                          if row.get("date_ms") and int(row["date_ms"]) <= end_ms
                          and (_date(row["date_ms"]) or "") < today_cn().isoformat())
        requests.append(str(payload.get("meta", {}).get("request_id", "")))

    def fetch_actions(bt):
        return _hithink(bt, "market", "corporate-actions", "--thscode", thscode,
                        "--from-date", start.isoformat(), "--to-date", end.isoformat())
    actions = _fetch_cached(f"{item['code']}_actions.json", fetch_actions, refresh, refresh_daily=True)
    requests.append(str(actions.get("meta", {}).get("request_id", "")))
    action_rows = [row for row in actions.get("data", {}).get("item", [])
                   if row.get("ex_date_ms") and start_ms <= int(row["ex_date_ms"]) <= end_ms]
    return {"item": item, "financial": financial, "prices": price_rows,
            "actions": action_rows,
            "request_ids": [request_id for request_id in requests if request_id],
            "fetched_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
            "source": "hithink-finance remote"}


def _pull_index(start: date, end: date, refresh: bool) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    request_ids: list[str] = []
    for year in range(start.year, end.year + 1):
        first, last = max(start, date(year, 1, 1)), min(end, date(year, 12, 31))
        def fetch_index(bt, first=first, last=last):
            return _hithink(bt, "index", "history", "--thscode", CSI300_CODE,
                            "--start-ms", str(_ms(first)), "--end-ms", str(_ms(last)))
        payload = _fetch_cached(f"csi300_prices_{year}.json", fetch_index, refresh,
                                refresh_daily=year == today_cn().year)
        rows.extend(row for row in payload.get("data", {}).get("item", [])
                    if row.get("date_ms") and (_date(row["date_ms"]) or "") < today_cn().isoformat())
        request_ids.append(str(payload.get("meta", {}).get("request_id", "")))
    return {"prices": rows, "request_ids": [x for x in request_ids if x], "thscode": CSI300_CODE,
            "basis": "CSI 300 price index; dividend return is excluded by source"}


def _pull_current_valuations(refresh: bool) -> dict[str, Any]:
    path = _raw_path("valuation_snapshot_current.json")
    same_day = path.exists() and datetime.fromtimestamp(path.stat().st_mtime, CN_TZ).date() == today_cn()
    if path.exists() and not refresh and same_day:
        return json.loads(path.read_text(encoding="utf-8"))
    try:
        from . import backtest as bt
    except ImportError:
        import backtest as bt
    symbols = ",".join(item["thscode"] for item in UNIVERSE)
    temporary = path.with_suffix(".pending.json")
    temporary.unlink(missing_ok=True)
    command = bt._app_objects()[1]() + ["--format", "json", "--source", "remote", "--no-input",
        "valuation", "snapshot", "--thscodes", symbols, "--output", str(temporary)]
    with bt.DATA_LOCK:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=90, encoding="utf-8", errors="replace")
    line = next((value for value in proc.stdout.splitlines() if value.startswith('{"ok":')), None)
    if not line:
        raise RuntimeError((proc.stderr or proc.stdout or "估值快照接口无响应")[:400])
    summary = json.loads(line)
    if not summary.get("ok"):
        raise RuntimeError(str(summary.get("error", {}).get("message", "估值快照查询失败")))
    if not temporary.exists():
        raise RuntimeError("估值快照接口没有写出完整响应文件")
    payload = json.loads(temporary.read_text(encoding="utf-8"))
    temporary.replace(path)
    return payload


def _period_number(row: dict[str, Any]) -> int | None:
    match = re.search(r"Q([1-4])", str(row.get("fiscal_period", "")).upper())
    if match:
        return int(match.group(1))
    return None


def _key(row: dict[str, Any]) -> tuple[int, int] | None:
    q, year = _period_number(row), row.get("fiscal_year")
    if q is None or year is None:
        return None
    return int(year), q


def _ordinal(period: tuple[int, int]) -> int:
    return int(period[0]) * 4 + int(period[1]) - 1


def _latest_rows_by_period(rows: list[dict[str, Any]]) -> dict[tuple[int, int], dict[str, Any]]:
    latest: dict[tuple[int, int], dict[str, Any]] = {}
    for row in sorted(rows, key=lambda item: int(item.get("report_date_ms") or 0)):
        period = _key(row)
        if period is not None:
            latest[period] = row
    return latest


def _quarters(rows: list[dict[str, Any]], fields: list[str]) -> dict[tuple[int, int], dict[str, float]]:
    cumulative: dict[tuple[int, int], dict[str, float]] = {}
    for row in sorted(rows, key=lambda item: int(item.get("report_date_ms") or 0)):
        period = _key(row)
        if period is None:
            continue
        cumulative[period] = {name: value for name in fields if (value := _num(row, name)) is not None}
    result: dict[tuple[int, int], dict[str, float]] = {}
    for period, values in cumulative.items():
        previous = (period[0], period[1] - 1) if period[1] > 1 else None
        result[period] = {}
        for name, value in values.items():
            prior = cumulative.get(previous, {}).get(name) if previous else 0.0
            # Financials and cash flow reports are year-to-date cumulative. A
            # missing prior quarter makes the single-quarter value unusable.
            if period[1] == 1:
                result[period][name] = value
            elif prior is not None:
                result[period][name] = value - prior
    return result


INCOME_FIELDS = ["operating_income", "operating_costs", "operating_profit", "profit_total", "net_profit", "parent_holder_net_profit"]
CASH_FIELDS = ["act_cash_flow_net", "pay_fixed_assets_etc_cash"]


def _bar_rows(raw: dict[str, Any]) -> list[dict[str, Any]]:
    by_date: dict[str, dict[str, Any]] = {}
    for item in raw.get("prices", []):
        try:
            day = _date(item.get("date_ms"))
            opn, close = float(item.get("open_price")), float(item.get("close_price"))
            if day and opn > 0 and close > 0 and math.isfinite(opn) and math.isfinite(close):
                row = {"date": day, "open": opn, "close": close}
                previous = by_date.get(day)
                if previous and (abs(previous["open"] - opn) > 1e-8 or abs(previous["close"] - close) > 1e-8):
                    raise RuntimeError(f"{raw.get('item', {}).get('code', 'symbol')} {day} has conflicting duplicate daily bars")
                by_date[day] = row
        except (TypeError, ValueError):
            continue
    return sorted(by_date.values(), key=lambda x: x["date"])


def _action_map(raw: dict[str, Any]) -> dict[str, list[dict[str, float]]]:
    output: dict[str, list[dict[str, float]]] = {}
    for event in raw.get("actions", []):
        day = _date(event.get("ex_date_ms"))
        if not day:
            continue
        output.setdefault(day, []).append({"cash": float(event.get("dividend_per_share") or 0),
                                           "bonus": float(event.get("per_share_bonus") or 0)})
    return output


def _last_close(bars: list[dict[str, Any]], day: str) -> float | None:
    row = _last_bar(bars, day)
    return float(row["close"]) if row else None


def _last_bar(bars: list[dict[str, Any]], day: str) -> dict[str, Any] | None:
    dates = [x["date"] for x in bars]
    index = bisect.bisect_right(dates, day) - 1
    return bars[index] if index >= 0 else None


def _snapshot_history(raw: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[dict[str, float]]]]:
    financial = raw.get("financial", {})
    income_rows = financial.get("income", [])
    balance_rows = financial.get("balance", [])
    cash_rows = financial.get("cashflow", [])
    income_q = _quarters(income_rows, INCOME_FIELDS)
    cash_q = _quarters(cash_rows, CASH_FIELDS)
    income_raw = _latest_rows_by_period(income_rows)
    balance = _latest_rows_by_period(balance_rows)
    cash_raw = _latest_rows_by_period(cash_rows)
    periods = sorted(set(income_q) & set(balance) & set(cash_q), key=_ordinal)
    bars = _bar_rows(raw)
    actions = _action_map(raw)
    snapshots = []
    for position, period in enumerate(periods):
        if position < 3:
            continue
        last_four = periods[position - 3:position + 1]
        if len(last_four) != 4 or any(_ordinal(b) - _ordinal(a) != 1 for a, b in zip(last_four, last_four[1:])):
            continue
        income_ttm: dict[str, float] = {}
        cash_ttm: dict[str, float] = {}
        for field in INCOME_FIELDS:
            values = [income_q.get(q, {}).get(field) for q in last_four]
            if all(v is not None for v in values):
                income_ttm[field] = sum(float(v) for v in values)
        for field in CASH_FIELDS:
            values = [cash_q.get(q, {}).get(field) for q in last_four]
            if all(v is not None for v in values):
                cash_ttm[field] = sum(float(v) for v in values)
        current_balance = balance[period]
        older_balance = balance.get(last_four[0])
        income_item, cash_item = income_raw.get(period, {}), cash_raw.get(period, {})
        disclosure_values = [int(row.get("report_date_ms") or 0) for row in (income_item, current_balance, cash_item)]
        if any(value <= 0 for value in disclosure_values):
            continue
        disclosure_ms = max(disclosure_values)
        disclosed = _date(disclosure_ms)
        if not disclosed:
            continue
        available = (date.fromisoformat(disclosed) + timedelta(days=1)).isoformat()
        revenue = income_ttm.get("operating_income")
        net_profit = income_ttm.get("net_profit")
        parent_profit = income_ttm.get("parent_holder_net_profit")
        assets = _num(current_balance, "assets_total")
        equity = _num(current_balance, "holder_equity_total")
        start_assets = _num(older_balance or {}, "assets_total")
        start_equity = _num(older_balance or {}, "holder_equity_total")
        cfo = cash_ttm.get("act_cash_flow_net")
        capex = cash_ttm.get("pay_fixed_assets_etc_cash")
        cost = income_ttm.get("operating_costs")
        gross_margin = _safe_div(revenue - cost, revenue) if revenue is not None and cost is not None else None
        net_margin = _safe_div(net_profit, revenue)
        average_assets = _safe_div((assets + start_assets), 2) if assets is not None and start_assets is not None else None
        average_equity = _safe_div((equity + start_equity), 2) if equity is not None and start_equity is not None else None
        asset_turnover = _safe_div(revenue, average_assets)
        leverage = _safe_div(average_assets, average_equity)
        roe = net_margin * asset_turnover * leverage if None not in (net_margin, asset_turnover, leverage) else None
        fcf = cfo - capex if cfo is not None and capex is not None else None
        basic_eps = _num(income_item, "basic_eps")
        # EPS is reported cumulatively on this feed; use the matching YTD
        # parent profit to estimate shares only where both inputs are usable.
        ytd_parent_profit = _num(income_item, "parent_holder_net_profit")
        share_ratio = _safe_div(ytd_parent_profit, basic_eps) if ytd_parent_profit is not None and basic_eps is not None and abs(basic_eps) > 1e-12 else None
        shares = share_ratio if share_ratio is not None and share_ratio > 0 else None
        bvps = _safe_div(equity, shares)
        current_debt = _num(current_balance, "total_debt")
        ar = _num(current_balance, "accounts_receivable")
        snapshots.append({
            "period": f"{period[0]}Q{period[1]}", "_period_ord": _ordinal(period), "period_end": _date(int(current_balance.get("period_end_ms") or 0)),
            "disclosed_date": disclosed, "available_date": available,
            "revenue_ttm": revenue, "net_profit_ttm": net_profit, "parent_profit_ttm": parent_profit,
            "operating_profit_ttm": income_ttm.get("operating_profit"), "cost_ttm": cost,
            "cfo_ttm": cfo, "capex_ttm": capex, "fcf_ttm": fcf,
            "assets": assets, "equity": equity, "average_assets": average_assets,
            "average_equity": average_equity, "total_debt_proxy": current_debt,
            "accounts_receivable": ar, "gross_margin": gross_margin, "net_margin": net_margin,
            "asset_turnover": asset_turnover, "financial_leverage": leverage, "dupont_roe": roe,
            "cash_conversion": _safe_div(cfo, net_profit), "fcf_margin": _safe_div(fcf, revenue),
            "debt_asset_proxy": _safe_div(current_debt, assets), "receivable_asset": _safe_div(ar, assets),
            "accrual_abs_to_assets": _safe_div(abs(net_profit - cfo), average_assets) if net_profit is not None and cfo is not None else None,
            "shares_approx": shares, "bvps_approx": bvps,
        })

    snapshot_by_ord = {row["_period_ord"]: row for row in snapshots}
    for row in snapshots:
        previous = snapshot_by_ord.get(row["_period_ord"] - 4)
        previous_3y = snapshot_by_ord.get(row["_period_ord"] - 12)
        row["revenue_growth_ttm_yoy"] = _growth(row.get("revenue_ttm"), previous.get("revenue_ttm") if previous else None)
        row["profit_growth_ttm_yoy"] = _growth(row.get("parent_profit_ttm"), previous.get("parent_profit_ttm") if previous else None)
        row["revenue_growth_3y_cagr"] = _growth_cagr(row.get("revenue_ttm"), previous_3y.get("revenue_ttm") if previous_3y else None, 3)
        row["profit_growth_3y_cagr"] = _growth_cagr(row.get("parent_profit_ttm"), previous_3y.get("parent_profit_ttm") if previous_3y else None, 3)
        row["gross_margin_change_yoy"] = (row.get("gross_margin") - previous.get("gross_margin")) if previous and row.get("gross_margin") is not None and previous.get("gross_margin") is not None else None
        row["eps_ttm_approx"] = _safe_div(row.get("parent_profit_ttm"), row.get("shares_approx"))
        row["eps_growth_ttm_yoy"] = _growth(row.get("eps_ttm_approx"), previous.get("eps_ttm_approx") if previous else None)
        row["fcf_yield_approx"] = None
    return snapshots, bars, actions


def _num(item: dict[str, Any], field: str) -> float | None:
    try:
        value = float(item[field])
        return value if math.isfinite(value) else None
    except (KeyError, TypeError, ValueError):
        return None


def _safe_div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or not math.isfinite(a) or not math.isfinite(b) or abs(b) < 1e-12:
        return None
    value = a / b
    return value if math.isfinite(value) else None


def _growth(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous <= 0:
        return None
    return current / previous - 1


def _growth_cagr(current: float | None, previous: float | None, years: int) -> float | None:
    if current is None or previous is None or current <= 0 or previous <= 0 or years <= 0:
        return None
    return (current / previous) ** (1 / years) - 1


def _total_return_bars(bars: list[dict[str, Any]], actions: dict[str, list[dict[str, float]]]) -> list[dict[str, Any]]:
    """Build a price-level total-return series using disclosed cash/bonus actions."""
    output: list[dict[str, Any]] = []
    previous_close = None
    index_value = 1.0
    for row in bars:
        close = float(row["close"])
        if previous_close is None:
            output.append({"date": row["date"], "close": index_value})
            previous_close = close
            continue
        events = actions.get(row["date"], [])
        cash_per_share = sum(float(event.get("cash", 0)) for event in events)
        bonus_factor = math.prod(1 + float(event.get("bonus", 0)) for event in events)
        gross_return = (close * bonus_factor + cash_per_share) / previous_close
        if math.isfinite(gross_return) and gross_return > 0:
            index_value *= gross_return
        output.append({"date": row["date"], "close": index_value})
        previous_close = close
    return output


def _volatility(bars: list[dict[str, Any]], day: str, window: int = 252,
                actions: dict[str, list[dict[str, float]]] | None = None) -> float | None:
    series = _total_return_bars(bars, actions or {})
    upto = [row for row in series if row["date"] <= day]
    if len(upto) < min(window, 126):
        return None
    closes = [row["close"] for row in upto[-(window + 1):]]
    returns = [b / a - 1 for a, b in zip(closes, closes[1:]) if a > 0]
    return statistics.pstdev(returns) * math.sqrt(252) if len(returns) >= min(window - 1, 125) else None


def _drawdown(bars: list[dict[str, Any]], day: str, window: int = 252,
              actions: dict[str, list[dict[str, float]]] | None = None) -> float | None:
    series = _total_return_bars(bars, actions or {})
    upto = [row for row in series if row["date"] <= day][-window:]
    if len(upto) < min(window, 126):
        return None
    peak, worst = 0.0, 0.0
    for row in upto:
        peak = max(peak, row["close"])
        if peak > 0:
            worst = min(worst, row["close"] / peak - 1)
    return worst


def _pct_ranks(rows: list[dict[str, Any]], field: str, higher_better: bool) -> dict[str, float]:
    valid = [(row["code"], row.get(field)) for row in rows if isinstance(row.get(field), (float, int)) and math.isfinite(float(row[field]))]
    if not valid:
        return {}
    valid.sort(key=lambda pair: float(pair[1]), reverse=higher_better)
    if len(valid) == 1:
        return {valid[0][0]: 50.0}
    ranks: dict[str, float] = {}
    i = 0
    while i < len(valid):
        j = i + 1
        while j < len(valid) and float(valid[j][1]) == float(valid[i][1]):
            j += 1
        positions = [100 * (len(valid) - 1 - idx) / (len(valid) - 1) for idx in range(i, j)]
        percentile = min(99.0, max(1.0, statistics.mean(positions)))
        for code, _ in valid[i:j]:
            ranks[code] = percentile
        i = j
    return ranks


FACTOR_SET = {
    "quality": [("dupont_roe", True), ("cash_conversion", True), ("fcf_margin", True), ("gross_margin", True)],
    "growth": [("revenue_growth_ttm_yoy", True), ("profit_growth_ttm_yoy", True), ("revenue_growth_3y_cagr", True), ("profit_growth_3y_cagr", True), ("gross_margin_change_yoy", True), ("eps_growth_ttm_yoy", True)],
    "valuation": [("pe_ttm_approx", False), ("pb_approx", False), ("ps_ttm_approx", False), ("pcf_ttm_approx", False), ("fcf_yield_approx", True)],
    "risk": [("debt_asset_proxy", False), ("receivable_asset", False), ("accrual_abs_to_assets", False), ("volatility_1y", False), ("drawdown_1y", True)],
}


def _rank_section(rows: list[dict[str, Any]], factor_names: list[tuple[str, bool]]) -> dict[str, dict[str, Any]]:
    rank_maps = {name: _pct_ranks(rows, name, higher) for name, higher in factor_names}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        values = [rank_maps[name][row["code"]] for name, _ in factor_names if row["code"] in rank_maps[name]]
        coverage = len(values) / len(factor_names) if factor_names else 0
        result[row["code"]] = {"score": round(sum(values) / len(values), 1) if values and coverage >= .8 else None,
                                "coverage": round(coverage, 2),
                                "factors": {name: {"value": row.get(name), "rank": rank_maps[name].get(row["code"]), "direction": "higher" if higher else "lower"}
                                            for name, higher in factor_names}}
    return result


def score_cross_section(raw_by_code: dict[str, dict[str, Any]], snapshot_map: dict[str, list[dict[str, Any]]],
                        day: str) -> list[dict[str, Any]]:
    rows = []
    for code, raw in raw_by_code.items():
        available = [s for s in snapshot_map.get(code, []) if s["available_date"] <= day]
        if not available:
            continue
        fundamentals = max(available, key=lambda x: x["available_date"])
        bars = _bar_rows(raw)
        quote = _last_bar(bars, day)
        price = float(quote["close"]) if quote else None
        item = BY_CODE[code]
        row = {"code": code, "name": item["name"], "sector": item["sector"], "model": item["model"], **fundamentals}
        row["price"] = price
        row["price_date"] = quote["date"] if quote else None
        eps = fundamentals.get("eps_ttm_approx")
        bvps = fundamentals.get("bvps_approx")
        row["pe_ttm_approx"] = _safe_div(price, eps) if eps is not None and eps > 0 else None
        row["pb_approx"] = _safe_div(price, bvps) if bvps is not None and bvps > 0 else None
        snapshot = raw.get("valuation_snapshot") if day == today_cn().isoformat() else None
        row["pe_ttm_current"] = _num(snapshot or {}, "pe_ttm")
        row["pb_mrq_current"] = _num(snapshot or {}, "pb_mrq")
        row["ps_ttm_current"] = _num(snapshot or {}, "ps_ttm")
        row["pcf_ttm_current"] = _num(snapshot or {}, "pcf_ttm")
        row["valuation_snapshot_timestamp_ms"] = (raw.get("valuation_snapshot_timestamp_ms") if snapshot else None)
        market_cap = price * fundamentals.get("shares_approx") if price and fundamentals.get("shares_approx") else None
        row["ps_ttm_approx"] = _safe_div(market_cap, fundamentals.get("revenue_ttm")) if (fundamentals.get("revenue_ttm") or 0) > 0 else None
        row["pcf_ttm_approx"] = _safe_div(market_cap, fundamentals.get("cfo_ttm")) if (fundamentals.get("cfo_ttm") or 0) > 0 else None
        row["fcf_yield_approx"] = _safe_div(fundamentals.get("fcf_ttm"), price * fundamentals.get("shares_approx")) if fundamentals.get("fcf_ttm") is not None and fundamentals.get("shares_approx") else None
        action_events = _action_map(raw)
        row["volatility_1y"] = _volatility(bars, day, actions=action_events)
        row["drawdown_1y"] = _drawdown(bars, day, actions=action_events)
        rows.append(row)

    # Financials deliberately use separate input sets. Provider data currently
    # omits NPL/provision/capital quality and insurer NBV/solvency, so those
    # sector scores remain unavailable rather than borrowing industrial ratios.
    ranked: dict[str, dict[str, Any]] = {}
    for section, factors in FACTOR_SET.items():
        regular = [row for row in rows if row["model"] not in {"bank", "insurer", "hybrid"}]
        ranked[section] = _rank_section(regular, factors)
    comparable_n = sum(1 for row in rows if row["model"] not in {"bank", "insurer", "hybrid"})
    output = []
    for row in rows:
        if row["model"] in {"bank", "insurer"}:
            model_note = "金融专项待补 NPL、拨备、资本充足率；保险另需 NBV/偿付能力，未套用工业企业 FCF 模型"
            sections = {k: {"score": None, "coverage": 0.0, "factors": {}, "status": "sector_inputs_missing"} for k in FACTOR_SET}
            sections["valuation"] = {"score": None, "coverage": 0.0, "factors": {
                "pb_approx": {"value": row.get("pb_approx"), "rank": None, "direction": "lower"},
                "dupont_roe": {"value": row.get("dupont_roe"), "rank": None, "direction": "higher"}}, "status": "sector_model_incomplete"}
        elif row["model"] == "hybrid":
            model_note = "航运与金融租赁混合业务；缺少分部口径，当前通用分数不适用"
            sections = {k: {"score": None, "coverage": 0.0, "factors": {}, "status": "segment_data_missing"} for k in FACTOR_SET}
        else:
            model_note = f"可比非金融研究样本 {comparable_n} 家（低于20家）；暂用跨行业研究池分位，仅作探索性排序，研究池存在存续偏差"
            sections = {section: {**ranked[section].get(row["code"], {"score": None, "coverage": 0, "factors": {}}), "status": "available"}
                        for section in FACTOR_SET}
        category_scores = [sections[k]["score"] for k in ("quality", "growth", "valuation", "risk")]
        category_coverage = [sections[k]["coverage"] for k in ("quality", "growth", "valuation", "risk")]
        overall = None
        if all(x is not None for x in category_scores) and all(x >= .8 for x in category_coverage):
            overall = round(sum(float(score) * weight for score, weight in zip(category_scores, (.35, .25, .25, .15))), 1)
        labels = []
        if sections["quality"]["score"] is not None and sections["quality"]["score"] >= 60:
            labels.append("quality candidate")
        if sections["growth"]["score"] is not None and sections["growth"]["score"] >= 60:
            labels.append("growth candidate")
        if sections["valuation"]["score"] is not None and sections["valuation"]["score"] >= 60:
            labels.append("valuation candidate")
        if overall is None:
            label = "needs more data"
        elif len(labels):
            label = " / ".join(labels)
        else:
            label = "watch"
        output.append({"code": row["code"], "name": row["name"], "sector": row["sector"], "sector_model": row["model"],
                       "price": row.get("price"), "period": row.get("period"), "disclosed_date": row.get("disclosed_date"),
                       "price_date": row.get("price_date"),
                       "revenue_ttm": row.get("revenue_ttm"), "profit_ttm": row.get("parent_profit_ttm"),
                       "dupont": {k: row.get(k) for k in ("net_margin", "asset_turnover", "financial_leverage", "dupont_roe")},
                       "cash_conversion": row.get("cash_conversion"), "fcf_margin": row.get("fcf_margin"),
                       "dupont_roe": row.get("dupont_roe"), "gross_margin": row.get("gross_margin"),
                        "revenue_growth_ttm_yoy": row.get("revenue_growth_ttm_yoy"),
                        "profit_growth_ttm_yoy": row.get("profit_growth_ttm_yoy"),
                        "revenue_growth_3y_cagr": row.get("revenue_growth_3y_cagr"),
                        "profit_growth_3y_cagr": row.get("profit_growth_3y_cagr"),
                       "gross_margin_change_yoy": row.get("gross_margin_change_yoy"),
                       "fcf_yield_approx": row.get("fcf_yield_approx"),
                       "debt_asset_proxy": row.get("debt_asset_proxy"), "receivable_asset": row.get("receivable_asset"),
                       "accrual_abs_to_assets": row.get("accrual_abs_to_assets"),
                       "profit_quality": {"operating_cash_flow_to_net_profit": row.get("cash_conversion"),
                                         "accrual_abs_to_average_assets": row.get("accrual_abs_to_assets"),
                                         "adjusted_profit_share": None, "government_subsidy_share": None,
                                         "investment_income_share": None, "fair_value_change_share": None,
                                         "share_compensation_share": None,
                                         "status": "incomplete_provider_statement_fields"},
                       "volatility_1y": row.get("volatility_1y"), "drawdown_1y": row.get("drawdown_1y"),
                       "eps_growth_ttm_yoy": row.get("eps_growth_ttm_yoy"),
                        "pe_ttm_approx": row.get("pe_ttm_approx"), "pb_approx": row.get("pb_approx"),
                        "ps_ttm_approx": row.get("ps_ttm_approx"), "pcf_ttm_approx": row.get("pcf_ttm_approx"),
                        "pe_ttm_current": row.get("pe_ttm_current"), "pb_mrq_current": row.get("pb_mrq_current"),
                        "ps_ttm_current": row.get("ps_ttm_current"), "pcf_ttm_current": row.get("pcf_ttm_current"),
                       "valuation_snapshot_timestamp_ms": row.get("valuation_snapshot_timestamp_ms"),
                        "growth": {k: row.get(k) for k in ("revenue_growth_ttm_yoy", "profit_growth_ttm_yoy", "revenue_growth_3y_cagr", "profit_growth_3y_cagr", "gross_margin_change_yoy")},
                       "ml_predictions": {"ridge": row.get("ml_ridge_score"), "lightgbm": row.get("ml_lightgbm_score")},
                       "sections": sections, "coverage": round(sum(category_coverage) / len(category_coverage), 2),
                       "score": overall, "label": label, "model_note": model_note, "peer_sample_n": comparable_n,
                       "missing": _missing_factors(row, sections)})
    return sorted(output, key=lambda x: (x["score"] is None, -(x["score"] or 0), x["code"]))


def _missing_factors(row: dict[str, Any], sections: dict[str, Any]) -> list[str]:
    missing = []
    for section, data in sections.items():
        for name, factor in data.get("factors", {}).items():
            if factor.get("value") is None:
                missing.append(f"{section}:{name}")
    if row.get("sector_model") == "bank":
        missing.extend(["NPL、不良贷款率", "拨备覆盖率", "核心一级资本充足率", "净息差"])
    if row.get("sector_model") == "insurer":
        missing.extend(["NBV/内含价值", "偿付能力充足率", "投资资产质量"])
    if row.get("sector_model") == "hybrid":
        missing.extend(["航运与金融租赁分部收入", "分部利润", "融资租赁资产质量"])
    missing.extend(["扣非利润/非经常损益拆分", "政府补助占比", "投资收益与公允价值变动拆分", "股份支付金额"])
    return list(dict.fromkeys(missing))


def _materialize(raw_by_code: dict[str, dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]], dict[str, dict[str, list[dict[str, float]]]]]:
    snapshots, bars, actions = {}, {}, {}
    for code, raw in raw_by_code.items():
        snapshots[code], bars[code], actions[code] = _snapshot_history(raw)
    return snapshots, bars, actions


def _quarter_signals(benchmark: list[dict[str, Any]], start: date, end: date) -> list[str]:
    rows = [row for row in benchmark if start.isoformat() <= row["date"] <= end.isoformat()]
    by_quarter: dict[tuple[int, int], str] = {}
    for row in rows:
        d = date.fromisoformat(row["date"])
        quarter = (d.month - 1) // 3 + 1
        by_quarter[(d.year, quarter)] = row["date"]
    return list(by_quarter.values())


def _benchmark_bars(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in payload.get("prices", []):
        day = _date(item.get("date_ms"))
        close = _num(item, "close_price")
        opn = _num(item, "open_price")
        if day and close and opn and close > 0 and opn > 0:
            rows.append({"date": day, "open": opn, "close": close})
    return sorted({x["date"]: x for x in rows}.values(), key=lambda x: x["date"])


def _industry_counts(targets: list[str]) -> bool:
    counts: dict[str, int] = {}
    finance_count = 0
    for code in targets:
        item = BY_CODE[code]
        counts[item["sector"]] = counts.get(item["sector"], 0) + 1
        finance_count += item["model"] in {"bank", "insurer", "hybrid"}
    return max(counts.values(), default=0) <= 3 and finance_count <= 2


def _targets_for_scores(rows: list[dict[str, Any]], top_n: int, eligible_only: bool = True) -> list[str]:
    chosen = []
    for row in rows:
        if row.get("score") is None:
            continue
        if eligible_only and (row.get("sections", {}).get("quality", {}).get("score") is None or
                              row["sections"]["quality"]["score"] < 60 or
                              row.get("sections", {}).get("risk", {}).get("score") is None or
                              row["sections"]["risk"]["score"] < 50 or
                              row.get("sections", {}).get("valuation", {}).get("score") is None or
                              row["sections"]["valuation"]["score"] < 50 or row.get("coverage", 0) < .8):
            continue
        candidate = chosen + [row["code"]]
        if _industry_counts(candidate):
            chosen = candidate
        if len(chosen) >= top_n:
            break
    return chosen


def _targets_for_model(rows: list[dict[str, Any]], score_field: str, top_n: int) -> list[str]:
    eligible = [row for row in rows if row.get(score_field) is not None and
                row.get("sections", {}).get("quality", {}).get("score") is not None and row["sections"]["quality"]["score"] >= 60 and
                row.get("sections", {}).get("risk", {}).get("score") is not None and row["sections"]["risk"]["score"] >= 50 and
                row.get("sections", {}).get("valuation", {}).get("score") is not None and row["sections"]["valuation"]["score"] >= 50 and
                row.get("coverage", 0) >= .8]
    eligible.sort(key=lambda row: (-float(row[score_field]), row["code"]))
    chosen = []
    for row in eligible:
        candidate = chosen + [row["code"]]
        if _industry_counts(candidate):
            chosen = candidate
        if len(chosen) >= top_n:
            break
    return chosen


def _add_ml_predictions(panel: list[dict[str, Any]], day: str, rows: list[dict[str, Any]],
                        model_status: dict[str, Any]) -> dict[str, int]:
    if not panel or model_status.get("status") != "available":
        return {}
    training = _matured_training(panel, day)
    feature_rows = [row for row in rows if row.get("price") is not None]
    results = {}
    model_configs = {"ridge": None}
    for meta in model_status.get("models", []):
        if meta.get("model") == "LightGBM":
            model_configs["lightgbm"] = meta.get("config")
    for model, config in model_configs.items():
        predictions = _fit_predict(training, feature_rows, model, config)
        if predictions is None:
            continue
        field = f"ml_{model}_score"
        for row, prediction in zip(feature_rows, predictions):
            row[field] = prediction
        results[model] = len(predictions)
    return results


def _fee(gross: float, side: str, day: str, code: str, req: ResearchRunRequest) -> float:
    commission = max(req.minimum_commission, gross * req.commission_rate)
    transfer = gross * req.transfer_rate if BY_CODE[code]["thscode"].endswith(".SH") else 0.0
    stamp_rate = .0005 if day >= "2023-08-28" else .001
    stamp = gross * stamp_rate if side == "sell" else 0.0
    return commission + transfer + stamp


def _xirr(flows: list[tuple[str, float]]) -> float | None:
    if len(flows) < 2 or not any(amount < 0 for _, amount in flows) or not any(amount > 0 for _, amount in flows):
        return None
    start = date.fromisoformat(flows[0][0])
    def npv(rate: float) -> float:
        total = 0.0
        for day, amount in flows:
            years = (date.fromisoformat(day) - start).days / 365.0
            total += amount / ((1 + rate) ** years)
        return total
    lo, hi = -.99, 10.0
    for _ in range(120):
        mid = (lo + hi) / 2
        if npv(mid) > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _consume_lots(lots: list[dict[str, Any]], shares: float, sale_date: str) -> float:
    remaining = shares
    tax_due = 0.0
    for lot in lots:
        original_shares = lot["shares"]
        taken = min(lot["shares"], remaining)
        basis = float(lot.get("dividend_basis", 0)) * taken / original_shares if original_shares else 0.0
        tax_due += basis * _dividend_tax_rate(lot["acquired"], sale_date)
        lot["dividend_basis"] = max(0.0, float(lot.get("dividend_basis", 0)) - basis)
        lot["shares"] -= taken
        remaining -= taken
        if remaining <= 1e-8:
            break
    lots[:] = [lot for lot in lots if lot["shares"] > 1e-8]
    return tax_due


def _add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _dividend_tax_rate(acquired: str, ex_date: str) -> float:
    acquired_day, assessed_day = date.fromisoformat(acquired), date.fromisoformat(ex_date)
    return .20 if assessed_day <= _add_months(acquired_day, 1) else .10 if assessed_day <= _add_months(acquired_day, 12) else 0.0


def _tax_liability(lots: dict[str, list[dict[str, Any]]], day: str) -> float:
    return sum(float(lot.get("dividend_basis", 0)) * _dividend_tax_rate(lot["acquired"], day)
               for company_lots in lots.values() for lot in company_lots)


def _simulate_portfolio(strategy: str, mode: Literal["fixed", "monthly"], req: ResearchRunRequest,
                        targets: dict[str, list[str]], bars: dict[str, list[dict[str, Any]]],
                        actions: dict[str, dict[str, list[dict[str, float]]]], benchmark: list[dict[str, Any]]) -> dict[str, Any]:
    all_days = sorted({row["date"] for row in benchmark if req.start_date.isoformat() <= row["date"] <= req.evaluation_date.isoformat()})
    for code_bars in bars.values():
        all_days = sorted(set(all_days) | {row["date"] for row in code_bars if req.start_date.isoformat() <= row["date"] <= req.evaluation_date.isoformat()})
    if not all_days:
        return {"status": "no_data", "strategy": strategy, "mode": mode}
    by_code = {code: {row["date"]: row for row in series} for code, series in bars.items()}
    benchmark_by_day = {row["date"]: row for row in benchmark}
    quarter_exec: dict[str, list[str]] = {}
    for signal, names in targets.items():
        idx = bisect.bisect_right(all_days, signal)
        if idx < len(all_days):
            quarter_exec.setdefault(all_days[idx], []).append(signal)
    month_first: set[str] = set()
    month_keys = set()
    for day in all_days:
        key = day[:7]
        if key not in month_keys:
            month_keys.add(key); month_first.add(day)
    cash = 0.0
    shares: dict[str, float] = {code: 0.0 for code in BY_CODE}
    lots: dict[str, list[dict[str, Any]]] = {code: [] for code in BY_CODE}
    last_close: dict[str, float] = {}
    total_fees = total_turnover = total_dividends = total_tax = 0.0
    units = 0.0
    flows: list[tuple[str, float]] = []
    curves = []
    curve_nav: list[float] = []
    ledger: list[dict[str, Any]] = []
    deposited = 0.0
    last_targets: list[str] = []
    current_nav = 1.0
    for day_index, day in enumerate(all_days):
        today_bars = {code: series.get(day) for code, series in by_code.items()}
        # Apply ex-date bonus shares and dividend cash to positions held overnight.
        for code, events in actions.items():
            for event in events.get(day, []):
                if shares.get(code, 0) <= 0:
                    continue
                for lot in lots[code]:
                    gross_dividend = lot["shares"] * event["cash"]
                    cash += gross_dividend
                    total_dividends += gross_dividend
                    lot["dividend_basis"] = float(lot.get("dividend_basis", 0)) + gross_dividend
                    lot["shares"] *= 1 + event["bonus"]
                shares[code] = sum(lot["shares"] for lot in lots[code])
                if event["cash"] or event["bonus"]:
                    ledger.append({"date": day, "type": "corporate_action", "code": code,
                                   "cash_per_share": event["cash"], "bonus_per_share": event["bonus"], "note": "除权日按数据源分红/送转信息入账"})

        # Missing bar means no executable order today; valuation may still use
        # the last completed close for a suspended/temporarily absent code.
        open_price = {code: (row["open"] if row else None) for code, row in today_bars.items()}
        open_mark = {code: (open_price.get(code) or last_close.get(code)) for code in shares}
        close_price = {code: (row["close"] if row else last_close.get(code)) for code, row in today_bars.items()}
        open_value = cash + sum(shares[code] * price for code, price in open_mark.items() if price is not None) - _tax_liability(lots, day)
        external = 0.0
        if mode == "fixed" and day_index == 0:
            external += req.initial_capital
        if mode == "monthly" and day in month_first:
            external += req.monthly_contribution
        if external:
            if units <= 0:
                current_nav = 1.0
                units = external
            else:
                nav_before = open_value / units if units else 1.0
                units += external / max(nav_before, 1e-8)
            cash += external
            deposited += external
            flows.append((day, -external))
            ledger.append({"date": day, "type": "contribution", "amount": round(external, 2), "note": "外部资金流入"})

        signal_days = quarter_exec.get(day, [])
        should_rebalance = bool(signal_days) or (mode == "monthly" and day in month_first) or (mode == "fixed" and day_index == 0)
        full_rebalance = bool(signal_days) or (mode == "fixed" and day_index == 0)
        if signal_days:
            last_targets = targets[signal_days[-1]]
        if should_rebalance and (last_targets or signal_days):
            # Sell overweights first. A-share orders are rounded down to one lot.
            tradable_targets = [c for c in last_targets if open_price.get(c) and open_price[c] > 0]
            mark_open = {c: (open_price.get(c) or last_close.get(c, 0)) for c in shares}
            equity_value = sum(shares[c] * mark_open.get(c, 0) for c in shares)
            total_value = cash + equity_value - _tax_liability(lots, day)
            target_weight = 1 / len(last_targets) if last_targets else 0
            if full_rebalance:
                for code in list(shares):
                    price = open_price.get(code)
                    if not price or shares[code] <= 0:
                        continue
                    desired = total_value * target_weight if code in tradable_targets else 0.0
                    current_value = shares[code] * price
                    excess = current_value - desired
                    full_exit = desired <= 0 and excess >= current_value - 1e-8
                    if not full_exit and excess <= price * 100:
                        continue
                    if full_exit:
                        sell_shares = shares[code]  # whole-position odd-lot disposal is allowed
                    else:
                        sell_shares = min(shares[code], math.floor(excess / price / 100) * 100)
                    if sell_shares <= 0:
                        continue
                    trade_price = price * (1 - req.slippage_rate)
                    gross = sell_shares * trade_price
                    fee = _fee(gross, "sell", day, code, req)
                    dividend_tax = _consume_lots(lots[code], sell_shares, day)
                    cash += gross - fee - dividend_tax
                    shares[code] -= sell_shares
                    total_fees += fee; total_turnover += gross; total_tax += dividend_tax
                    ledger.append({"date": day, "type": "sell", "code": code, "shares": sell_shares,
                                   "price": round(trade_price, 4), "gross": round(gross, 2), "fee": round(fee, 2),
                                   "dividend_tax": round(dividend_tax, 2)})
            mark_open = {c: (open_price.get(c) or last_close.get(c, 0)) for c in shares}
            total_value = cash + sum(shares[c] * mark_open.get(c, 0) for c in shares) - _tax_liability(lots, day)
            target_weight = 1 / len(last_targets) if last_targets else 0
            for code in tradable_targets:
                price = open_price[code]
                desired = total_value * target_weight
                deficit = desired - shares[code] * price
                buy_price = price * (1 + req.slippage_rate)
                max_lots = max(0, math.floor(max(0, min(deficit, cash)) / (buy_price * 100)))
                while max_lots > 0:
                    gross = max_lots * 100 * buy_price
                    fee = _fee(gross, "buy", day, code, req)
                    if gross + fee <= cash + 1e-8:
                        break
                    max_lots -= 1
                if max_lots <= 0:
                    continue
                bought = max_lots * 100
                gross = bought * buy_price
                fee = _fee(gross, "buy", day, code, req)
                cash -= gross + fee
                shares[code] += bought
                lots[code].append({"shares": float(bought), "acquired": day, "dividend_basis": 0.0})
                total_fees += fee; total_turnover += gross
                ledger.append({"date": day, "type": "buy", "code": code, "shares": bought,
                               "price": round(buy_price, 4), "gross": round(gross, 2), "fee": round(fee, 2)})

        close_value = cash + sum(shares[code] * (close_price.get(code) or last_close.get(code, 0)) for code in shares) - _tax_liability(lots, day)
        for code, price in close_price.items():
            if price and price > 0:
                last_close[code] = price
        if units <= 0:
            continue
        current_nav = close_value / units
        curve_nav.append(current_nav)
        curves.append({"date": day, "value": round(close_value, 2), "nav": round(current_nav, 8),
                       "invested": round(deposited, 2), "cash": round(cash, 2)})
    if not curves:
        return {"status": "no_trades", "strategy": strategy, "mode": mode, "ledger": ledger}
    end_day, final_value = curves[-1]["date"], curves[-1]["value"]
    flows.append((end_day, final_value))
    first_flow_day = next((day for day, amount in flows if amount < 0), curves[0]["date"])
    returns = [curve_nav[0] - 1.0] + [b / a - 1 for a, b in zip(curve_nav, curve_nav[1:]) if a > 0]
    peak, max_dd = 1.0, 0.0
    for nav in [1.0, *curve_nav]:
        peak = max(peak, nav)
        max_dd = min(max_dd, nav / peak - 1 if peak else 0)
    elapsed_years = max(1 / 252, (date.fromisoformat(end_day) - date.fromisoformat(first_flow_day)).days / 365.25)
    twr_cagr = curve_nav[-1] ** (1 / elapsed_years) - 1 if curve_nav[-1] > 0 else None
    sharpe = statistics.mean(returns) / statistics.pstdev(returns) * math.sqrt(252) if len(returns) > 1 and statistics.pstdev(returns) else None
    positions = []
    for code, amount in shares.items():
        if amount > 0:
            price = last_close.get(code, 0)
            positions.append({"code": code, "name": BY_CODE[code]["name"], "shares": round(amount, 4),
                              "price": price, "value": round(amount * price, 2)})
    positions.sort(key=lambda x: x["value"], reverse=True)
    average_asset_value = statistics.mean(point["value"] for point in curves)
    turnover_year = total_turnover / max(elapsed_years, 1e-8) / max(average_asset_value, 1.0)
    return {"status": "done", "strategy": strategy, "mode": mode,
            "summary": {"initial_capital": req.initial_capital if mode == "fixed" else 0,
                        "monthly_contribution": req.monthly_contribution if mode == "monthly" else 0,
                        "deposited": round(deposited, 2), "final_value": round(final_value, 2),
                        "profit_vs_contributions": round(final_value - deposited, 2),
                        "simple_return_vs_contributions": final_value / deposited - 1 if deposited else None,
                        "xirr": _xirr(flows), "twr_cagr": twr_cagr, "max_drawdown": max_dd,
                        "annual_volatility": statistics.pstdev(returns) * math.sqrt(252) if len(returns) > 1 else None,
                        "sharpe_rf0": sharpe, "annual_turnover": turnover_year,
                        "fees": round(total_fees, 2), "dividends_gross": round(total_dividends, 2),
                        "dividend_tax_paid": round(total_tax, 2), "dividend_tax_liability_at_end": round(_tax_liability(lots, end_day), 2), "cash": round(cash, 2),
                        "actual_start": curves[0]["date"], "actual_end": end_day,
                        "top_position_weight": positions[0]["value"] / final_value if positions and final_value else None},
            "positions": positions, "curve": curves, "ledger": ledger,
            "cost_assumptions": {"commission_rate": req.commission_rate, "minimum_commission": req.minimum_commission,
                                 "stamp_duty_sell": "0.10% through 2023-08-27; 0.05% from 2023-08-28 (model assumption)",
                                 "transfer_rate": req.transfer_rate, "slippage_rate": req.slippage_rate,
                                 "dividend_tax": "20% within one calendar month; 10% after one month through one year; exempt after one year; tax provided as a liability and deducted on sales (lot approximation)",
                                 "tax_sources": ["https://gtm-cn-uqm3dbhc20k.guangdong.chinatax.gov.cn/gdsw/zjfg/2015-09/09/content_2aca9985150c439fb119b1f8a277b857.shtml",
                                                 "https://tianjin.chinatax.gov.cn/11200000000/0300/030005/20220725150235434.shtml",
                                                 "https://www.mof.gov.cn/jrttts/202308/t20230828_3904235.htm"]}}


ML_FEATURES = ["dupont_roe", "cash_conversion", "fcf_margin", "gross_margin", "revenue_growth_ttm_yoy",
               "profit_growth_ttm_yoy", "gross_margin_change_yoy", "pe_ttm_approx", "pb_approx",
               "fcf_yield_approx", "debt_asset_proxy", "receivable_asset", "accrual_abs_to_assets", "volatility_1y", "drawdown_1y"]


def _read_ml_panel() -> tuple[list[dict[str, Any]], str]:
    path = RUNTIME / "csi300_pit_training.csv"
    if not path.exists():
        return [], f"未发现 {path.name}；ML挑战模型暂停，避免用当前成分股或未来数据训练。"
    required = {"signal_date", "label_end_date", "thscode", "in_csi300", "forward_excess_252d", *ML_FEATURES}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        if not rows or not required.issubset(rows[0]):
            return [], "CSI300点时训练CSV缺少必要的目标或因子列；ML模型未运行。"
        valid = []
        for row in rows:
            try:
                row["signal_date"] = str(row["signal_date"])[:10]
                row["label_end_date"] = str(row["label_end_date"])[:10]
                row["in_csi300"] = str(row["in_csi300"]).strip().lower() in {"1", "true", "yes"}
                row["forward_excess_252d"] = float(row["forward_excess_252d"])
                for field in ML_FEATURES:
                    raw_value = row.get(field)
                    row[field] = float(raw_value) if raw_value not in (None, "") else None
                if (row["signal_date"] and row["label_end_date"] > row["signal_date"] and row["in_csi300"]
                        and math.isfinite(row["forward_excess_252d"])):
                    valid.append(row)
            except (TypeError, ValueError):
                continue
        return valid, f"读入 {len(valid)} 行含点时成分身份和成熟标签的CSI300样本。"
    except (OSError, csv.Error) as exc:
        return [], f"读取CSI300点时训练CSV失败：{exc}"


def _make_estimator(model: str, config: dict[str, Any] | None = None):
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    if model == "ridge":
        from sklearn.linear_model import Ridge
        return make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), Ridge(alpha=1.0))
    from lightgbm import LGBMRegressor
    params = config or {"num_leaves": 15, "learning_rate": .05, "min_child_samples": 100}
    return make_pipeline(SimpleImputer(strategy="median"), LGBMRegressor(
        n_estimators=300, num_leaves=params["num_leaves"], learning_rate=params["learning_rate"],
        min_child_samples=params["min_child_samples"], random_state=42, verbosity=-1, n_jobs=1))


def _fit_predict(training: list[dict[str, Any]], targets: list[dict[str, Any]], model: str,
                 config: dict[str, Any] | None = None) -> list[float] | None:
    if len(training) < 200 or not targets:
        return None
    try:
        import numpy as np
        estimator = _make_estimator(model, config)
        x_train = [[row.get(name) if row.get(name) is not None else np.nan for name in ML_FEATURES] for row in training]
        y_train = [float(row["forward_excess_252d"]) for row in training]
        x_target = [[row.get(name) if row.get(name) is not None else np.nan for name in ML_FEATURES] for row in targets]
        estimator.fit(x_train, y_train)
        return [float(x) for x in estimator.predict(x_target)]
    except (ImportError, ValueError, TypeError):
        return None


def _rank_values(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i + 1
        while j < len(order) and values[order[j]] == values[order[i]]:
            j += 1
        rank = (i + j - 1) / 2
        for pos in range(i, j):
            ranks[order[pos]] = rank
        i = j
    return ranks


def _spearman(predicted: list[float], actual: list[float]) -> float | None:
    if len(predicted) < 3:
        return None
    a, b = _rank_values(predicted), _rank_values(actual)
    mean_a, mean_b = statistics.mean(a), statistics.mean(b)
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    den_a = math.sqrt(sum((x - mean_a) ** 2 for x in a))
    den_b = math.sqrt(sum((y - mean_b) ** 2 for y in b))
    return cov / (den_a * den_b) if den_a and den_b else None


def _matured_training(panel: list[dict[str, Any]], cutoff: str) -> list[dict[str, Any]]:
    return [row for row in panel if row["in_csi300"] and row["signal_date"] < cutoff and row["label_end_date"] < cutoff]


def _oos_metrics(panel: list[dict[str, Any]], model: str, config: dict[str, Any] | None = None,
                 test_start: str = "2024-01-01") -> dict[str, Any]:
    dates = sorted({row["signal_date"] for row in panel if row["signal_date"] >= test_start})
    ics, top_returns, samples, used_dates = [], [], 0, 0
    for signal in dates:
        train = _matured_training(panel, signal)
        test = [row for row in panel if row["signal_date"] == signal and row["in_csi300"]]
        if len(test) < 15:
            continue
        prediction = _fit_predict(train, test, model, config)
        if prediction is None:
            continue
        actual = [row["forward_excess_252d"] for row in test]
        ic = _spearman(prediction, actual)
        if ic is not None:
            ics.append(ic); used_dates += 1; samples += len(test)
        count = max(1, math.ceil(len(test) * .2))
        winners = sorted(range(len(test)), key=lambda idx: prediction[idx], reverse=True)[:count]
        top_returns.extend(actual[idx] for idx in winners)
    return {"model": "Ridge" if model == "ridge" else "LightGBM", "oos_start": test_start,
            "signal_dates": used_dates, "samples": samples,
            "mean_rank_ic": statistics.mean(ics) if ics else None,
            "top_quintile_mean_excess_return": statistics.mean(top_returns) if top_returns else None,
            "status": "experimental" if used_dates < 12 else "oos_available",
            "config": config or {"alpha": 1.0}}


def _select_lgbm_config(panel: list[dict[str, Any]]) -> dict[str, Any] | None:
    try:
        import lightgbm  # noqa: F401
    except ImportError:
        return None
    configs = [{"num_leaves": leaves, "learning_rate": rate, "min_child_samples": child}
               for leaves in (7, 15) for rate in (.03, .05) for child in (100, 300)]
    dates = sorted({row["signal_date"] for row in panel if "2021-01-01" <= row["signal_date"] < "2024-01-01"})
    best, best_score = None, -math.inf
    for config in configs:
        ics = []
        for signal in dates:
            train = _matured_training(panel, signal)
            valid = [row for row in panel if row["signal_date"] == signal and row["in_csi300"]]
            if len(valid) < 15:
                continue
            prediction = _fit_predict(train, valid, "lightgbm", config)
            if prediction is None:
                continue
            ic = _spearman(prediction, [row["forward_excess_252d"] for row in valid])
            if ic is not None:
                ics.append(ic)
        if ics and statistics.mean(ics) > best_score:
            best, best_score = config, statistics.mean(ics)
    return best


def _ml_challengers(include_ml: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not include_ml:
        return [], {"status": "disabled", "message": "本次未请求机器学习挑战模型。"}
    panel, message = _read_ml_panel()
    if not panel:
        return [], {"status": "unavailable", "message": message, "models": ["Ridge", "LightGBM"]}
    try:
        import sklearn  # noqa: F401
    except ImportError:
        return panel, {"status": "unavailable", "message": f"{message} 尚未安装scikit-learn。", "models": ["Ridge", "LightGBM"]}
    ridge = _oos_metrics(panel, "ridge")
    lgbm_config = _select_lgbm_config(panel)
    models = [ridge]
    if lgbm_config:
        models.append(_oos_metrics(panel, "lightgbm", lgbm_config))
    return panel, {"status": "available", "message": message, "rows": len(panel), "feature_set": ML_FEATURES,
                   "models": models, "selection": "LightGBM仅在2021-2023验证窗按平均rank IC选参；2024起作为锁定样本评估。",
                   "promotion_rule": "只有样本外净年化高于基线、最大回撤恶化不超过3个百分点、年换手不超过400%，且费用翻倍仍占优才可晋级。"}


def _build_report(run_id: str, request: ResearchRunRequest, raw_by_code: dict[str, dict[str, Any]],
                  index_payload: dict[str, Any], ml_status: dict[str, Any], ml_panel: list[dict[str, Any]],
                  valuation_payload: dict[str, Any] | None) -> dict[str, Any]:
    snapshot_map, bars, actions = _materialize(raw_by_code)
    benchmark = _benchmark_bars(index_payload)
    evaluation = min(request.evaluation_date, today_cn()).isoformat()
    current_rows = score_cross_section(raw_by_code, snapshot_map, evaluation)
    current_ml_counts = _add_ml_predictions(ml_panel, evaluation, current_rows, ml_status)
    signal_days = _quarter_signals(benchmark, request.start_date, request.evaluation_date)
    target_sets: dict[str, dict[str, list[str]]] = {"factor_top8": {}, "universe_equal_weight": {}}
    if ml_status.get("status") == "available":
        target_sets["ridge_top8"] = {}
        if any(item.get("model") == "LightGBM" for item in ml_status.get("models", [])):
            target_sets["lightgbm_top8"] = {}
    signal_diagnostics = []
    factor_history = []
    for day in signal_days:
        scored = score_cross_section(raw_by_code, snapshot_map, day)
        _add_ml_predictions(ml_panel, day, scored, ml_status)
        top = _targets_for_scores(scored, request.top_n)
        for row in scored:
            factor_history.append({
                "date": day, "code": row["code"], "name": row["name"],
                "score": row.get("score"), "label": row.get("label"),
                "pe_ttm_approx": row.get("pe_ttm_approx"), "pb_approx": row.get("pb_approx"),
                "ps_ttm_approx": row.get("ps_ttm_approx"), "pcf_ttm_approx": row.get("pcf_ttm_approx"),
                "dupont_roe": row.get("dupont_roe"),
                "revenue_growth_3y_cagr": row.get("revenue_growth_3y_cagr"),
                "profit_growth_3y_cagr": row.get("profit_growth_3y_cagr"),
                "sections": {key: value.get("score") for key, value in row.get("sections", {}).items()},
            })
        target_sets["factor_top8"][day] = top
        for model in ("ridge", "lightgbm"):
            field = f"ml_{model}_score"
            if f"{model}_top8" in target_sets:
                target_sets[f"{model}_top8"][day] = _targets_for_model(scored, field, request.top_n)
        # Fixed research pool is intentionally disclosed as survivorship-biased.
        available = [code for code in sorted(raw_by_code) if _last_close(bars.get(code, []), day) is not None]
        target_sets["universe_equal_weight"][day] = available
        signal_diagnostics.append({"date": day, "factor_candidates": top, "universe_count": len(available)})

    modes = ["fixed", "monthly"] if request.investment_mode == "both" else [request.investment_mode]
    backtests = []
    for mode in modes:
        for strategy, targets in target_sets.items():
            if not any(day_targets for day_targets in targets.values()):
                backtests.append({"status": "no_eligible_signals", "strategy": strategy, "mode": mode,
                                  "message": "没有符合质量/估值/风险门槛的历史信号，资金保持现金。"})
                continue
            backtests.append(_simulate_portfolio(strategy, mode, request, targets, bars, actions, benchmark))

    benchmark_result = _benchmark_summary(benchmark, request.start_date.isoformat(), evaluation)
    coverage = []
    for code, raw in raw_by_code.items():
        row = next((x for x in current_rows if x["code"] == code), None)
        all_bars = bars.get(code, [])
        coverage.append({"code": code, "name": BY_CODE[code]["name"], "sector": BY_CODE[code]["sector"],
                         "source": raw.get("source"), "price_first": all_bars[0]["date"] if all_bars else None,
                         "price_last": all_bars[-1]["date"] if all_bars else None, "price_rows": len(all_bars),
                         "financial_snapshots": len(snapshot_map.get(code, [])),
                         "request_ids": raw.get("request_ids", []), "score_status": "available" if row and row.get("score") is not None else "incomplete"})
    latest_report_dates = [x.get("disclosed_date") for x in current_rows if x.get("disclosed_date")]
    return {"run_id": run_id, "as_of": evaluation, "model_version": MODEL_VERSION,
            "method": "DuPont + quality/growth/valuation/risk cross-sectional percentile baseline",
            "point_in_time_status": "approx_point_in_time; provider amendments/vintages unavailable",
            "research_pool": [dict(item) for item in UNIVERSE],
            "summary": {"companies": len(UNIVERSE),
                        "companies_with_data": sum(bool(snapshot_map.get(item["code"])) for item in UNIVERSE),
                        "companies_fetch_succeeded": len(raw_by_code),
                        "companies_scored": sum(1 for row in current_rows if row.get("score") is not None),
                        "requested_start": request.start_date.isoformat(),
                        "price_data_start": min((row["price_first"] for row in coverage if row.get("price_first")), default=None),
                        "price_data_end": max((row["price_last"] for row in coverage if row.get("price_last")), default=None),
                        "latest_financial_disclosure": max(latest_report_dates) if latest_report_dates else None,
                        "valuation_snapshot_time_ms": (valuation_payload or {}).get("data", {}).get("timestamp"),
                        "benchmark": benchmark_result, "signal_dates": len(signal_days)},
            "companies": current_rows, "coverage": coverage, "backtests": backtests,
            "signal_diagnostics": signal_diagnostics, "factor_history": factor_history,
            "ml": {**ml_status, "current_predictions": current_ml_counts},
            "data_provenance": {"provider": "Hithink Finance", "fetched_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
                                "request_ids": {code: raw.get("request_ids", []) for code, raw in raw_by_code.items()},
                                "index_request_ids": index_payload.get("request_ids", []),
                                "valuation_request_id": (valuation_payload or {}).get("meta", {}).get("request_id"),
                                "valuation_snapshot_timestamp_ms": (valuation_payload or {}).get("data", {}).get("timestamp"),
                                "benchmark_basis": index_payload.get("basis", "CSI 300 price index"),
                                "price_basis": "unadjusted OHLC + disclosed cash dividend/bonus actions in portfolio simulation",
                                "universe_bias": "固定25只研究名单存在存续/事后选样偏差；不得视作可交易历史成分池"},
            "limitations": ["历史财报更正版本不可回溯，点时仅按披露日近似。", "P/E、P/B以财报EPS/股数推算，存在股本变化误差。",
                            "金融股缺少NPL、拨备、资本充足率、净息差或NBV/偿付能力，专项评分保留缺失。",
                            "历史固定25只股票池存在存续偏差；ML面板未就绪时不提供模型回测。",
                            "非金融可比研究样本不足20家；横截面分位是探索性指标，尚未接入CSI300完整财务截面。",
                            "CSI 300指数接口返回价格指数；分红回报未计入基准，不能称为全收益基准。",
                            "红利税、印花税和佣金按报告中列示的简化历史假设计算；券商实际费率可能不同。"]}


def _benchmark_summary(bars: list[dict[str, Any]], start: str, end: str) -> dict[str, Any]:
    rows = [row for row in bars if start <= row["date"] <= end]
    if len(rows) < 2:
        return {"status": "insufficient_data", "thscode": CSI300_CODE, "basis": "price_return"}
    first, last = rows[0], rows[-1]
    years = max(1 / 252, (date.fromisoformat(last["date"]) - date.fromisoformat(first["date"])).days / 365.25)
    total = last["close"] / first["close"] - 1
    return {"status": "available", "thscode": CSI300_CODE, "basis": "price_return_only",
            "start": first["date"], "end": last["date"], "total_return": total,
            "cagr": (last["close"] / first["close"]) ** (1 / years) - 1}


def _ten_years_ago(day: date) -> date:
    try:
        return day.replace(year=day.year - 10)
    except ValueError:
        return day.replace(year=day.year - 10, day=28)


def execute_run(run_id: str, request: ResearchRunRequest, path: Path = RUN_DB) -> None:
    initialize(path)
    _set_run(run_id, path, status="running", progress=1, message="准备研究数据")
    try:
        end = min(request.evaluation_date, today_cn())
        start = max(request.start_date, _ten_years_ago(end))
        raw_by_code: dict[str, dict[str, Any]] = {}
        errors = []
        for index, item in enumerate(UNIVERSE, start=1):
            _set_run(run_id, path, progress=5 + 65 * (index - 1) / len(UNIVERSE), message=f"正在更新 {item['name']} ({index}/{len(UNIVERSE)})")
            try:
                raw_by_code[item["code"]] = _pull_symbol(
                    item, start, end, request.refresh_data,
                    progress=lambda message, value: _set_run(run_id, path, progress=value, message=message),
                    index=index, total=len(UNIVERSE))
            except Exception as exc:
                errors.append({"code": item["code"], "name": item["name"], "error": str(exc)[:500]})
        _set_run(run_id, path, progress=73, message="获取沪深300基准历史")
        try:
            index_payload = _pull_index(start, end, request.refresh_data)
        except Exception as exc:
            index_payload = {"prices": [], "request_ids": [], "thscode": CSI300_CODE,
                             "basis": "unavailable", "error": str(exc)[:500]}
            errors.append({"code": CSI300_CODE, "name": "沪深300基准", "error": str(exc)[:500]})
        valuation_payload = None
        if end == today_cn():
            _set_run(run_id, path, progress=78, message="获取当前估值快照")
            try:
                candidate = _pull_current_valuations(request.refresh_data)
                timestamp = candidate.get("data", {}).get("timestamp")
                if _date(timestamp) == end.isoformat():
                    valuation_payload = candidate
                    items = {row.get("thscode"): row for row in candidate.get("data", {}).get("item", [])}
                    for item in UNIVERSE:
                        raw = raw_by_code.get(item["code"])
                        if raw and item["thscode"] in items:
                            raw["valuation_snapshot"] = items[item["thscode"]]
                            raw["valuation_snapshot_timestamp_ms"] = timestamp
                else:
                    errors.append({"code": "valuation.snapshot", "name": "当前估值快照",
                                   "error": "服务端估值快照时间不属于评估日，未用于展示。"})
            except Exception as exc:
                errors.append({"code": "valuation.snapshot", "name": "当前估值快照", "error": str(exc)[:500]})
        _set_run(run_id, path, progress=82, message="计算点时因子与DuPont分解")
        ml_panel, ml_status = _ml_challengers(request.include_ml)
        report = _build_report(run_id, request, raw_by_code, index_payload, ml_status, ml_panel, valuation_payload)
        report["errors"] = errors
        _set_run(run_id, path, status="done", progress=100, message="研究完成", result=report)
        _write_html_export(run_id, report)
    except Exception as exc:
        _set_run(run_id, path, status="failed", message="研究任务失败", error=f"{type(exc).__name__}: {exc}")


def _write_html_export(run_id: str, report: dict[str, Any]) -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = EXPORT_DIR / f"stock-research-{run_id}.html"
    rows = report.get("companies", [])
    headings = "<tr><th>公司</th><th>行业</th><th>当前 PE</th><th>当前 PB</th><th>当前 PS</th><th>当前 PCF</th><th>历史估算 PE</th><th>历史估算 PB</th><th>历史估算 PS</th><th>历史估算 PCF</th><th>ROE</th><th>质量</th><th>成长</th><th>估值</th><th>风险</th><th>结论</th></tr>"
    body = "".join("<tr>" + "".join(f"<td>{_html(row.get(key))}</td>" for key in ("name", "sector", "pe_ttm_current", "pb_mrq_current", "ps_ttm_current", "pcf_ttm_current", "pe_ttm_approx", "pb_approx", "ps_ttm_approx", "pcf_ttm_approx")) +
                   "".join(f"<td>{_html(row.get('dupont', {}).get('dupont_roe') if key == 'roe' else row.get('sections', {}).get(section, {}).get('score'))}</td>"
                           for key, section in (("roe", "quality"), ("quality", "quality"), ("growth", "growth"), ("valuation", "valuation"), ("risk", "risk"))) +
                   f"<td>{_html(row.get('label'))}</td></tr>" for row in rows)
    history_rows = "".join(f"<tr><td>{_html(point.get('date'))}</td><td>{_html(point.get('code'))} {_html(point.get('name'))}</td><td>{_html(point.get('score'))}</td><td>{_html(point.get('sections', {}).get('quality'))}</td><td>{_html(point.get('sections', {}).get('growth'))}</td><td>{_html(point.get('sections', {}).get('valuation'))}</td><td>{_html(point.get('sections', {}).get('risk'))}</td><td>{_html(point.get('pe_ttm_approx'))}</td><td>{_html(point.get('pb_approx'))}</td><td>{_html(point.get('ps_ttm_approx'))}</td><td>{_html(point.get('pcf_ttm_approx'))}</td><td>{_html(point.get('revenue_growth_3y_cagr'))}</td><td>{_html(point.get('profit_growth_3y_cagr'))}</td></tr>" for point in report.get("factor_history", []))
    html = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>股票量化研究 {run_id[:8]}</title>
<style>body{{font:15px/1.6 system-ui,'Microsoft YaHei',sans-serif;color:#1d3838;background:#f3f7f4;margin:0}}main{{max-width:1400px;margin:auto;padding:24px}}section{{background:white;border:1px solid #dce7e2;border-radius:14px;padding:18px;margin:16px 0}}table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{padding:8px;border-bottom:1px solid #e3eae7;text-align:left}}th{{background:#f5f8f6}}.muted{{color:#657a77}}</style><main><h1>股票量化研究与回测</h1><p>研究日期：{_html(report.get('as_of'))} · 模型：{_html(report.get('model_version'))} · 运行：{_html(run_id)}</p>
<section><h2>研究池评分</h2><div style="overflow:auto"><table>{headings}{body}</table></div></section><section><h2>历史因子</h2><div style="overflow:auto"><table><tr><th>信号日</th><th>公司</th><th>总分</th><th>质量</th><th>成长</th><th>估值</th><th>风险</th><th>PE估算</th><th>PB估算</th><th>PS估算</th><th>PCF估算</th><th>营收3年CAGR</th><th>利润3年CAGR</th></tr>{history_rows}</table></div></section><section><h2>数据覆盖与限制</h2><p class="muted">{_html(report.get('point_in_time_status'))}；{_html(report.get('data_provenance', {}).get('benchmark_basis'))}；估值请求号：{_html(report.get('data_provenance', {}).get('valuation_request_id'))}</p><ul>{''.join(f'<li>{_html(x)}</li>' for x in report.get('limitations', []))}</ul></section>
<section><h2>回测</h2><pre>{_html(json.dumps(report.get('backtests', []), ensure_ascii=False, indent=2)[:40000])}</pre></section></main></html>"""
    path.write_text(html, encoding="utf-8")
    return path


def _html(value: Any) -> str:
    text = "—" if value is None else (f"{value:.4f}" if isinstance(value, float) else str(value))
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;")


def companies_csv(report: dict[str, Any]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["code", "name", "sector", "price", "price_date", "PE_TTM_current", "PB_MRQ_current", "PS_TTM_current", "PCF_TTM_current", "valuation_snapshot_timestamp_ms", "PE_TTM_approx", "PB_approx", "PS_TTM_approx", "PCF_TTM_approx", "ROE_DuPont", "revenue_growth_3y_cagr", "profit_growth_3y_cagr",
                     "cash_conversion", "accrual_abs_to_assets", "adjusted_profit_share", "government_subsidy_share",
                     "investment_income_share", "fair_value_change_share", "share_compensation_share",
                     "quality_score", "growth_score", "valuation_score", "risk_score", "coverage", "score", "label", "missing"])
    for row in report.get("companies", []):
        sections = row.get("sections", {})
        quality = row.get("profit_quality", {})
        writer.writerow([row.get("code"), row.get("name"), row.get("sector"), row.get("price"), row.get("price_date"), row.get("pe_ttm_current"), row.get("pb_mrq_current"), row.get("ps_ttm_current"), row.get("pcf_ttm_current"), row.get("valuation_snapshot_timestamp_ms"), row.get("pe_ttm_approx"), row.get("pb_approx"), row.get("ps_ttm_approx"), row.get("pcf_ttm_approx"),
                          row.get("dupont", {}).get("dupont_roe"), row.get("revenue_growth_3y_cagr"), row.get("profit_growth_3y_cagr"), row.get("cash_conversion"), quality.get("accrual_abs_to_average_assets"),
                         quality.get("adjusted_profit_share"), quality.get("government_subsidy_share"), quality.get("investment_income_share"),
                         quality.get("fair_value_change_share"), quality.get("share_compensation_share"),
                         sections.get("quality", {}).get("score"), sections.get("growth", {}).get("score"),
                         sections.get("valuation", {}).get("score"), sections.get("risk", {}).get("score"), row.get("coverage"), row.get("score"), row.get("label"), ";".join(row.get("missing", []))])
    return "\ufeff" + output.getvalue()


def factor_history_csv(report: dict[str, Any]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["signal_date", "code", "name", "score", "quality", "growth", "valuation", "risk",
                     "PE_TTM_approx", "PB_approx", "PS_TTM_approx", "PCF_TTM_approx", "ROE_DuPont",
                     "revenue_growth_3y_cagr", "profit_growth_3y_cagr", "label"])
    for row in report.get("factor_history", []):
        sections = row.get("sections", {})
        writer.writerow([row.get("date"), row.get("code"), row.get("name"), row.get("score"),
                         sections.get("quality"), sections.get("growth"), sections.get("valuation"),
                          sections.get("risk"), row.get("pe_ttm_approx"), row.get("pb_approx"),
                          row.get("ps_ttm_approx"), row.get("pcf_ttm_approx"),
                          row.get("dupont_roe"), row.get("revenue_growth_3y_cagr"),
                          row.get("profit_growth_3y_cagr"), row.get("label")])
    return "\ufeff" + output.getvalue()
