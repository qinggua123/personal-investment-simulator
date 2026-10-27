"""Historical, independent research ledger. No paper-trading account is touched."""
from __future__ import annotations

import bisect
from contextlib import closing
import json
import math
import sqlite3
import subprocess
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator

HERE = Path(__file__).resolve().parent


def _app_objects():
    try:
        from .app import INSTRUMENTS, cli_base_command
    except ImportError:  # uvicorn app:app when launched from this directory
        from app import INSTRUMENTS, cli_base_command
    return INSTRUMENTS, cli_base_command

HISTORY_DIR = HERE / "runtime" / "history"
RUN_DB = HERE / "runtime" / "backtests.sqlite3"
DATA_LOCK = threading.Lock()
CN_TZ = ZoneInfo("Asia/Shanghai")


def today_cn() -> date:
    return datetime.now(CN_TZ).date()


class Allocation(BaseModel):
    code: str
    amount: float = Field(gt=0, le=1_000_000)


class CustomEntry(Allocation):
    date: date


class BacktestRequest(BaseModel):
    strategy: str = "monthly"
    start_date: date
    stop_date: date
    evaluation_date: date
    allocations: list[Allocation] = Field(default_factory=list)
    custom_entries: list[CustomEntry] = Field(default_factory=list)
    commission_rate: float = Field(default=0.0003, ge=0, le=0.02)
    min_commission: float = Field(default=5, ge=0, le=100)
    fund_fee_rate: float = Field(default=0.0012, ge=0, le=0.1)
    bond_fee_rate: float = Field(default=0.0005, ge=0, le=0.1)
    slippage_rate: float = Field(default=0, ge=0, le=0.05)
    reinvest_dividends: bool = True
    refresh_data: bool = False

    @model_validator(mode="after")
    def check(self):
        if self.strategy not in {"once", "weekly", "monthly", "custom"}:
            raise ValueError("策略必须为一次性、每周、每月或自定义")
        if not (self.start_date <= self.stop_date <= self.evaluation_date <= today_cn()):
            raise ValueError("日期必须满足开始 ≤ 停投 ≤ 评估 ≤ 今天")
        if self.evaluation_date - self.start_date > timedelta(days=365 * 12):
            raise ValueError("单次回测最长 12 年")
        entries = self.custom_entries if self.strategy == "custom" else self.allocations
        if not entries or len(entries) > 250:
            raise ValueError("请选择 1 至 250 条投资配置")
        if any(x.code not in _app_objects()[0] for x in entries):
            raise ValueError("包含未支持的标的")
        if self.strategy == "custom" and any(not self.start_date <= x.date <= self.stop_date for x in entries):
            raise ValueError("自定义买入日期必须在投入期间")
        if self.strategy != "custom" and len({x.code for x in entries}) != len(entries):
            raise ValueError("重复标的请合并每期金额")
        return self


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, CN_TZ).date().isoformat()


def _ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=CN_TZ).timestamp() * 1000)


def _cli(*args: str) -> dict[str, Any]:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    command = _app_objects()[1]() + ["--format", "json", "--source", "remote", "--db", str(HISTORY_DIR / "market.duckdb"), "--no-input", *args]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=90, encoding="utf-8", errors="replace")
    line = next((s for s in proc.stdout.splitlines() if s.startswith('{"ok":')), None)
    if not line:
        raise RuntimeError((proc.stderr or proc.stdout or "数据接口无响应")[:400])
    payload = json.loads(line)
    if not payload.get("ok"):
        raise RuntimeError(str(payload.get("error", {}).get("message", "历史数据查询失败")))
    if payload.get("meta", {}).get("truncated"):
        raise RuntimeError("数据源截断了历史结果，请缩短区间")
    return payload


def _suffix(code: str) -> str:
    return code + (".OF" if _app_objects()[0][code]["kind"] == "fund" else ".SH" if code.startswith("6") or code == "513800" else ".SZ")


def _cache_path(code: str, key: str) -> Path:
    return HISTORY_DIR / f"{code}_{key}.json"


def _cached(code: str, key: str, fetch, *, force: bool = False) -> dict[str, Any]:
    path = _cache_path(code, key)
    live_key = key in {"nav_fyear", "actions", "dividends"} or key == f"raw_{today_cn().year}"
    same_cn_day = path.exists() and datetime.fromtimestamp(path.stat().st_mtime, CN_TZ).date() == today_cn()
    if path.exists() and not force and (not live_key or same_cn_day):
        return json.loads(path.read_text(encoding="utf-8"))
    payload = fetch()
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return payload


def load_history(code: str, start: date, end: date, *, refresh: bool = False) -> dict[str, Any]:
    kind = _app_objects()[0][code]["kind"]
    symbol = _suffix(code)
    request_ids: list[str] = []
    with DATA_LOCK:  # the CLI can lock its local DuckDB, even with --source remote
        if kind == "fund":
            raw = _cached(code, "nav_fyear", lambda: _cli("fund", "nav", "--fund-type", "otc", "--thscode", symbol, "--range", "fyear", "--nav-type", "unit"), force=refresh)
            request_ids.append(str(raw.get("meta", {}).get("request_id", "")))
            items = raw["data"].get("item", [])
            rows = [{"date": _day(int(x["nav_date"])), "open": float(x["unit_nav"]), "close": float(x["unit_nav"])} for x in items if x.get("unit_nav")]
        else:
            rows = []
            for year in range(start.year, end.year + 1):
                first, last = date(year, 1, 1), date(year + 1, 1, 1) - timedelta(days=1)
                key = f"raw_{year}"
                def fetch_year(first=first, last=last):
                    params = ["--thscode", symbol, "--start-ms", str(_ms(first)), "--end-ms", str(_ms(last)), "--adjust", "none"]
                    return _cli(*( ["fund", "history", *params[:-2]] if kind == "etf" else ["market", "history", *params]))
                raw = _cached(code, key, fetch_year, force=refresh)
                request_ids.append(str(raw.get("meta", {}).get("request_id", "")))
                for x in raw["data"].get("item", []):
                    if x.get("open_price") and x.get("close_price"):
                        rows.append({"date": _day(int(x["date_ms"])), "open": float(x["open_price"]), "close": float(x["close_price"])})
        if kind == "stock":
            raw_events = _cached(code, "actions", lambda: _cli("market", "corporate-actions", "--thscode", symbol, "--from-date", "2015-01-01", "--to-date", today_cn().isoformat()), force=refresh)
            request_ids.append(str(raw_events.get("meta", {}).get("request_id", "")))
            events = [{"date": _day(int(x["ex_date_ms"])), "cash": float(x.get("dividend_per_share") or 0), "bonus": float(x.get("per_share_bonus") or 0), "payment": None} for x in raw_events["data"].get("item", []) if x.get("ex_date_ms")]
        else:
            raw_events = _cached(code, "dividends", lambda: _cli("fund", "dividends", "--fund-type", "otc" if kind == "fund" else "exchange", "--thscode", symbol), force=refresh)
            request_ids.append(str(raw_events.get("meta", {}).get("request_id", "")))
            events = [{"date": _day(int(x["ex_dividend_date_ms"])), "registration": _day(int(x["registration_date_ms"])) if x.get("registration_date_ms") else None,
                       "cash": float(x.get("per_ten_cash_after_tax") if x.get("per_ten_cash_after_tax") is not None else x.get("per_ten_cash_before_tax") or 0) / 10, "bonus": 0.0,
                       "payment": _day(int(x["payment_date_ms"])) if x.get("payment_date_ms") else None} for x in raw_events["data"].get("item", []) if x.get("ex_dividend_date_ms")]
    # A same-day daily bar may still be in progress. Use completed prior days only.
    rows = sorted({r["date"]: r for r in rows if r["date"] < today_cn().isoformat()}.values(), key=lambda r: r["date"])
    if not rows:
        raise ValueError(f"{code} 无有效历史价格/净值")
    # Reject unresolved missing data within the observed interval. Chinese holidays can span ~10 days.
    for a, b in zip(rows, rows[1:]):
        if (date.fromisoformat(b["date"]) - date.fromisoformat(a["date"])).days > 16:
            raise ValueError(f"{code} 历史数据在 {a['date']} 至 {b['date']} 存在超过 16 天的缺口")
    return {"code": code, "kind": kind, "rows": rows, "events": events, "source": "hithink-finance remote", "request_ids": [x for x in request_ids if x],
            "first": rows[0]["date"], "last": rows[-1]["date"], "price_basis": "单位净值" if kind == "fund" else "不复权开盘/收盘"}


def _schedule(req: BacktestRequest) -> list[tuple[str, str, float]]:
    if req.strategy == "custom":
        return [(x.date.isoformat(), x.code, x.amount) for x in req.custom_entries]
    days = []
    if req.strategy == "once":
        days = [req.start_date]
    elif req.strategy == "weekly":
        d = req.start_date
        while d <= req.stop_date:
            days.append(d)
            d += timedelta(days=7)
    else:
        d = req.start_date
        while d <= req.stop_date:
            days.append(d)
            d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return [(d.isoformat(), x.code, x.amount) for d in days for x in req.allocations]


def _xirr(flows: list[tuple[str, float]]) -> float | None:
    if not flows or not any(v < 0 for _, v in flows) or not any(v > 0 for _, v in flows):
        return None
    first = date.fromisoformat(flows[0][0])
    def npv(rate):
        return sum(v / ((1 + rate) ** ((date.fromisoformat(d) - first).days / 365.25)) for d, v in flows)
    lo, hi = -0.9999, 1.0
    while npv(lo) * npv(hi) > 0 and hi < 1e5:
        hi = hi * 2 + 1
    if npv(lo) * npv(hi) > 0:
        return None
    for _ in range(100):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def calculate(req: BacktestRequest, histories: dict[str, dict[str, Any]]) -> dict[str, Any]:
    codes = sorted(histories)
    first = max(h["first"] for h in histories.values())
    last = min(h["last"] for h in histories.values())
    if (date.fromisoformat(first) - req.start_date).days > 16:
        raise ValueError(f"投入开始日早于共同数据起点 {first}；请调整日期或标的")
    if (req.evaluation_date - date.fromisoformat(last)).days > 16:
        raise ValueError(f"评估日超过共同已完成数据日 {last}；请调整日期")
    effective_end = min(req.evaluation_date.isoformat(), last)
    maps = {c: {r["date"]: r for r in histories[c]["rows"]} for c in codes}
    date_lists = {c: sorted(maps[c]) for c in codes}
    events = {c: {} for c in codes}
    for c in codes:
        for e in histories[c]["events"]:
            events[c].setdefault(e["date"], []).append(e)
    schedule = []
    for planned, code, amount in _schedule(req):
        dates = date_lists[code]
        idx = bisect.bisect_left(dates, planned)
        if idx >= len(dates) or dates[idx] > effective_end:
            raise ValueError(f"{code} 在 {planned} 后无可用交易/净值日")
        if (date.fromisoformat(dates[idx]) - date.fromisoformat(planned)).days > 16:
            raise ValueError(f"{code} 的 {planned} 投入日存在过长数据缺口")
        schedule.append((dates[idx], planned, code, amount))
    scheduled = {}
    for actual, planned, code, amount in schedule:
        scheduled.setdefault(actual, []).append((planned, code, amount, True))
    registration_days = {e["registration"] for c in codes for e in histories[c]["events"] if e.get("registration") and req.start_date.isoformat() <= e["registration"] <= effective_end}
    event_days = {d for c in codes for e in histories[c]["events"] for d in (e.get("date"), e.get("payment")) if d and req.start_date.isoformat() <= d <= effective_end}
    all_days = sorted({d for c in codes for d in maps[c] if req.start_date.isoformat() <= d <= effective_end} | set(scheduled) | registration_days | event_days)
    def queue_reinvestment(day: str, code: str, amount: float):
        dates = date_lists[code]
        idx = bisect.bisect_left(dates, day)
        if idx < len(dates) and dates[idx] <= effective_end:
            scheduled.setdefault(dates[idx], []).append((day, code, amount, False))
    units = {c: 0.0 for c in codes}
    unspent = {c: 0.0 for c in codes}
    marks: dict[str, float] = {}
    cash = invested = fees = dividends = 0.0
    ledger = []
    curve = []
    flows = []
    twr_nav = 1.0
    peak = 1.0
    max_dd = 0.0
    previous_value = 0.0
    pending_payments: dict[str, list[tuple[str, float]]] = {}
    entitlements: dict[tuple[str, str], float] = {}
    warnings = ["场外基金按当日单位净值确认，真实申购确认日和费率以平台为准", "股票现金分红缺支付日，按除权日入账；回测并非可执行成交记录", "场内假定能以开盘价成交，未模拟涨停无卖盘、停牌或流动性冲击", "历史限购、暂停申购、IOPV 和实时溢价未纳入，不能据此推定当时可成交"]
    if effective_end < req.evaluation_date.isoformat():
        warnings.insert(0, f"评估日已按全部选中标的的共同最新完整数据日截到 {effective_end}")
    for day in all_days:
        if day > req.evaluation_date.isoformat():
            break
        for c in codes:
            if day in maps[c]:
                marks[c] = maps[c][day]["close"]
        incoming = sum(x[2] for x in scheduled.get(day, []) if x[3])
        for c, paid in pending_payments.pop(day, []):
            cash += paid
            dividends += paid
            ledger.append({"date": day, "code": c, "type": "分红", "amount": round(paid, 2), "note": "按基金支付日入账"})
            if req.reinvest_dividends:
                queue_reinvestment(day, c, paid)
        for c in codes:
            for e in events[c].get(day, []):
                if units[c] <= 0:
                    continue
                old_units = units[c]
                eligible_units = entitlements.get((c, e["date"]), old_units)
                if e["bonus"]:
                    units[c] *= 1 + e["bonus"]
                    ledger.append({"date": day, "code": c, "type": "送转", "units": round(units[c] - old_units, 6)})
                if e["cash"]:
                    paid = eligible_units * e["cash"]
                    if histories[c]["kind"] == "stock" or not e.get("payment") or e["payment"] <= day:
                        cash += paid
                        dividends += paid
                        ledger.append({"date": day, "code": c, "type": "分红", "amount": round(paid, 2), "note": "股票按除权日入账" if histories[c]["kind"] == "stock" else "按基金支付日入账"})
                        if req.reinvest_dividends:
                            queue_reinvestment(day, c, paid)
                    else:
                        pending_payments.setdefault(e["payment"], []).append((c, paid))
        if previous_value > 0:
            receivable = sum(v for pay_date, entries in pending_payments.items() if pay_date > day for _, v in entries)
            pre = cash + receivable + sum(units[c] * marks.get(c, 0) for c in codes)
            twr_nav *= pre / previous_value
        cash += incoming
        invested += incoming
        if incoming:
            flows.append((day, -incoming))
        todays = scheduled.get(day, [])
        for planned, c, amount, external in todays:
            unspent[c] += amount
        for c in dict.fromkeys(x[1] for x in todays):
            planned = next(x[0] for x in todays if x[1] == c)
            row = maps[c].get(day)
            if not row:
                continue
            kind = histories[c]["kind"]
            spend = min(cash, unspent[c])
            if spend <= 0:
                continue
            price = row["open"] * (1 + req.slippage_rate) if kind != "fund" else row["close"]
            if kind == "fund":
                fund_rate = req.bond_fee_rate if c == "012692" else req.fund_fee_rate
                bought = spend / (1 + fund_rate) / price
                fee = spend - spend / (1 + fund_rate)
                cost = spend
            else:
                lot = 100
                per_lot = lot * price
                n_lots = math.floor(spend / per_lot)
                while n_lots > 0:
                    value = n_lots * per_lot
                    fee = max(req.min_commission, value * req.commission_rate)
                    if value + fee <= spend + 1e-8:
                        break
                    n_lots -= 1
                if n_lots == 0:
                    ledger.append({"date": day, "planned_date": planned, "code": c, "type": "未成交", "amount": round(spend, 2), "note": "不足一手及费用，资金留在现金"})
                    continue
                bought = n_lots * lot
                cost = bought * price + fee
            units[c] += bought
            cash -= cost
            unspent[c] -= cost
            fees += fee
            ledger.append({"date": day, "planned_date": planned, "code": c, "type": "买入", "price": round(price, 6), "units": round(bought, 6), "amount": round(cost, 2), "fee": round(fee, 2)})
        for c in codes:
            for e in histories[c]["events"]:
                if e.get("registration") == day:
                    entitlements[(c, e["date"])] = units[c]
        receivable = sum(v for pay_date, entries in pending_payments.items() if pay_date > day for _, v in entries)
        value = cash + receivable + sum(units[c] * marks.get(c, 0) for c in codes)
        previous_value = value
        peak = max(peak, twr_nav)
        max_dd = min(max_dd, twr_nav / peak - 1)
        curve.append({"date": day, "value": round(value, 2), "invested": round(invested, 2), "twr_nav": round(twr_nav, 6), "cash": round(cash, 2)})
    if not curve:
        raise ValueError("所选日期无可计算数据")
    actual_end = curve[-1]["date"]
    final_value = curve[-1]["value"]
    flows.append((actual_end, final_value))
    xirr = _xirr(flows)
    positions = [{"code": c, "name": _app_objects()[0][c]["name"], "units": round(units[c], 6), "last_price": marks.get(c), "market_value": round(units[c] * marks.get(c, 0), 2), "pending_budget": round(max(0, unspent[c]), 2)} for c in codes]
    return {"summary": {"invested": round(invested, 2), "value": round(final_value, 2), "profit": round(final_value - invested, 2), "simple_return": final_value / invested - 1 if invested else None,
                        "xirr": xirr, "twr_return": twr_nav - 1, "max_drawdown": max_dd, "cash": round(cash, 2), "fees": round(fees, 2), "dividends": round(dividends, 2), "actual_start": curve[0]["date"], "actual_end": actual_end},
            "coverage": [{k: h[k] for k in ("code", "source", "first", "last", "price_basis")} | {"rows": len(h["rows"]), "events": len(h["events"]), "request_ids": h.get("request_ids", [])} for h in histories.values()],
            "positions": positions, "curve": curve, "ledger": ledger, "warnings": warnings}


def init_runs(path: Path = RUN_DB):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, created_at TEXT NOT NULL, status TEXT NOT NULL, request_json TEXT NOT NULL, result_json TEXT, error TEXT)")


def create_run(req: BacktestRequest, path: Path = RUN_DB) -> str:
    init_runs(path)
    run_id = uuid.uuid4().hex
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("INSERT INTO runs(id,created_at,status,request_json) VALUES(?,?,?,?)", (run_id, datetime.now(timezone.utc).isoformat(), "running", req.model_dump_json()))
    return run_id


def execute_run(run_id: str, req: BacktestRequest, path: Path = RUN_DB):
    try:
        codes = {x.code for x in (req.custom_entries if req.strategy == "custom" else req.allocations)}
        histories = {c: load_history(c, req.start_date, req.evaluation_date, refresh=req.refresh_data) for c in sorted(codes)}
        result = calculate(req, histories)
        status, error = "done", None
    except Exception as exc:
        result, status, error = None, "failed", str(exc)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE runs SET status=?,result_json=?,error=? WHERE id=?", (status, json.dumps(result, ensure_ascii=False) if result else None, error, run_id))


def get_run(run_id: str, path: Path = RUN_DB) -> dict[str, Any]:
    init_runs(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        raise HTTPException(404, "回测记录不存在")
    return {"id": row["id"], "created_at": row["created_at"], "status": row["status"], "request": json.loads(row["request_json"]), "result": json.loads(row["result_json"]) if row["result_json"] else None, "error": row["error"]}
