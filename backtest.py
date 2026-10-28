"""Historical, independent research ledger. No paper-trading account is touched."""
from __future__ import annotations

import bisect
import calendar
from contextlib import closing
from decimal import Decimal, ROUND_DOWN
import hashlib
import json
import math
import sqlite3
import subprocess
import threading
from types import SimpleNamespace
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
    schedule_day: str = Field(default="1", pattern=r"^(?:[1-9]|1[0-9]|2[0-8]|last)$")
    schedule_weekday: int | None = Field(default=None, ge=0, le=4)
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
        if self.strategy not in {"once", "daily", "weekly", "biweekly", "monthly", "quarterly", "semiannual", "annual", "custom"}:
            raise ValueError("策略必须为每日、每周、每两周、每月、每季度、每半年、每年、一次性或自定义")
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


class LongDcaRequest(BaseModel):
    """Single-fund, long-history DCA example; independent of paper account."""
    start_date: date = date(2016, 1, 1)
    stop_date: date
    evaluation_date: date
    monthly_amount: Decimal = Field(gt=0, le=1_000_000)
    frequency: str = "monthly"
    schedule_day: str = Field(default="1", pattern=r"^(?:[1-9]|1[0-9]|2[0-8]|last)$")
    schedule_weekday: int = Field(default=0, ge=0, le=4)
    fee_rate: Decimal = Field(default=Decimal("0.0012"), ge=0, le=Decimal("0.1"))
    reinvest_dividends: bool = True
    refresh_data: bool = False

    @model_validator(mode="after")
    def check(self):
        if self.frequency not in {"once", "daily", "weekly", "biweekly", "monthly", "quarterly", "semiannual", "annual"}:
            raise ValueError("单基金定投频率不受支持")
        if not (self.start_date <= self.stop_date <= self.evaluation_date <= today_cn()):
            raise ValueError("日期必须满足开始 ≤ 停止投入 ≤ 评估 ≤ 今天")
        if self.evaluation_date - self.start_date > timedelta(days=365 * 20):
            raise ValueError("单次定投回测最长 20 年")
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
    live_key = key in {"nav_fyear", "nav_since_inception", "actions", "dividends", "splits_since_inception"} or key == f"raw_{today_cn().year}"
    same_cn_day = path.exists() and datetime.fromtimestamp(path.stat().st_mtime, CN_TZ).date() == today_cn()
    if path.exists() and not force and (not live_key or same_cn_day):
        return json.loads(path.read_text(encoding="utf-8"))
    payload = fetch()
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return payload


def _akshare_fund_nav(code: str) -> dict[str, Any]:
    """Fetch the full unit-NAV series. Hithink fund.nav is intentionally limited to its recent window."""
    try:
        import akshare as ak
    except ImportError as exc:
        raise RuntimeError("缺少 AKShare。请在项目环境安装 requirements.txt 后重试") from exc
    frame = _akshare_fund_info(ak, code, "单位净值走势")
    if frame is None or frame.empty or "净值日期" not in frame or "单位净值" not in frame:
        raise RuntimeError(f"AKShare 未返回 {code} 的完整单位净值序列")
    items = []
    for _, row in frame.iterrows():
        day = str(row["净值日期"])[:10]
        try:
            nav = float(row["单位净值"])
            parsed = date.fromisoformat(day)
        except (TypeError, ValueError):
            continue
        if math.isfinite(nav) and nav > 0:
            items.append({"nav_date": _ms(parsed), "unit_nav": nav})
    if not items:
        raise RuntimeError(f"AKShare 返回的 {code} 单位净值记录无效")
    items.sort(key=lambda x: x["nav_date"])
    return {"data": {"item": items}, "meta": {"source": "AKShare Eastmoney", "count": len(items)}}


def _akshare_fund_splits(code: str) -> dict[str, Any]:
    """Fetch unit conversion dates and factors; malformed split tables fail closed."""
    try:
        import akshare as ak
    except ImportError as exc:
        raise RuntimeError("缺少 AKShare，无法核验基金份额拆分事件") from exc
    frame = _akshare_fund_info(ak, code, "拆分详情")
    if frame is None or frame.empty:
        return {"data": {"item": []}, "meta": {"source": "AKShare Eastmoney", "count": 0}}
    required = {"拆分折算日", "拆分折算比例"}
    if not required.issubset(frame.columns):
        raise RuntimeError(f"AKShare 返回的 {code} 拆分表字段不完整，不能确认拆分记录")
    items = []
    for _, row in frame.iterrows():
        split_day = str(row["拆分折算日"])[:10]
        ratio = str(row["拆分折算比例"]).strip().replace("：", ":")
        try:
            day = date.fromisoformat(split_day)
            left, right = ratio.split(":", 1)
            factor = Decimal(right) / Decimal(left)
        except Exception as exc:
            raise RuntimeError(f"AKShare 返回无法解析的 {code} 拆分记录：{split_day} {ratio}") from exc
        if not factor.is_finite() or factor <= 0:
            raise RuntimeError(f"AKShare 返回无效的 {code} 拆分比例：{ratio}")
        items.append({"date": day.isoformat(), "factor": str(factor)})
    return {"data": {"item": items}, "meta": {"source": "AKShare Eastmoney", "count": len(items)}}


def _akshare_fund_info(ak, code: str, indicator: str):
    """Respect configured proxy first, then retry with a process-local direct session."""
    try:
        return ak.fund_open_fund_info_em(symbol=code, indicator=indicator, period="成立来")
    except Exception as proxy_error:
        try:
            import akshare.fund.fund_em as fund_api

            original_requests = fund_api.requests
            session = original_requests.Session()
            session.trust_env = False

            def direct_get(*args, **kwargs):
                kwargs.setdefault("timeout", 20)
                return session.get(*args, **kwargs)

            fund_api.requests = SimpleNamespace(get=direct_get)
            try:
                return ak.fund_open_fund_info_em(symbol=code, indicator=indicator, period="成立来")
            finally:
                fund_api.requests = original_requests
                session.close()
        except Exception as direct_error:
            raise RuntimeError(f"AKShare {code} {indicator} 请求失败（代理与直连均失败）：{proxy_error}; {direct_error}") from direct_error


def _cross_check_fund_nav(code: str, ak_items: list[dict[str, Any]]) -> tuple[dict[str, Any], str | None]:
    """Compare common dates with the cached/retrieved Hithink five-year NAV window."""
    key = "nav_fyear"
    cache = _cache_path(code, key)
    try:
        if cache.exists():
            hithink = json.loads(cache.read_text(encoding="utf-8"))
        else:
            hithink = _cached(code, key, lambda: _cli("fund", "nav", "--fund-type", "otc", "--thscode", _suffix(code), "--range", "fyear", "--nav-type", "unit"))
    except Exception as exc:
        return {"source": "hithink-finance", "status": "unavailable", "overlap": 0}, f"无法取得 Hithink 重叠期净值核对：{exc}"
    ak_by_day = {_day(int(x["nav_date"])): float(x["unit_nav"]) for x in ak_items if x.get("unit_nav")}
    h_by_day: dict[str, float] = {}
    for item in hithink.get("data", {}).get("item", []):
        if not item.get("nav_date") or not item.get("unit_nav"):
            continue
        day, nav = _day(int(item["nav_date"])), float(item["unit_nav"])
        prior = h_by_day.get(day)
        if prior is not None and abs(prior - nav) > 1e-8:
            raise ValueError(f"Hithink 在 {day} 存在冲突的重复净值")
        h_by_day[day] = nav
    common = sorted(set(ak_by_day) & set(h_by_day))
    if not common:
        return {"source": "hithink-finance", "status": "no_overlap", "overlap": 0}, "AKShare 与 Hithink 净值没有共同日期，无法交叉核验。"
    max_diff = max(abs(ak_by_day[day] - h_by_day[day]) for day in common)
    if max_diff > 0.0001:
        raise ValueError(f"AKShare 与 Hithink 的 {code} 单位净值冲突：共同日期 {len(common)} 天，最大差异 {max_diff:.6f}")
    checked = {"source": "hithink-finance", "status": "matched", "overlap": len(common), "max_abs_diff": round(max_diff, 8), "first": common[0], "last": common[-1]}
    return checked, None


def load_history(code: str, start: date, end: date, *, refresh: bool = False) -> dict[str, Any]:
    kind = _app_objects()[0][code]["kind"]
    symbol = _suffix(code)
    needs_full_history = kind == "fund" and code == "050025" and start <= today_cn() - timedelta(days=365 * 5)
    request_ids: list[str] = []
    cross_check = None
    with DATA_LOCK:  # the CLI can lock its local DuckDB, even with --source remote
        if kind == "fund":
            nav_key = "nav_since_inception" if needs_full_history else "nav_fyear"
            raw = _cached(code, nav_key, lambda: _akshare_fund_nav(code) if needs_full_history else _cli("fund", "nav", "--fund-type", "otc", "--thscode", symbol, "--range", "fyear", "--nav-type", "unit"), force=refresh)
            request_ids.append(str(raw.get("meta", {}).get("request_id", "")))
            items = raw["data"].get("item", [])
            rows = [{"date": _day(int(x["nav_date"])), "open": float(x["unit_nav"]), "close": float(x["unit_nav"])} for x in items if x.get("unit_nav")]
            if needs_full_history:
                cross_check, cross_warning = _cross_check_fund_nav(code, items)
                if cross_warning:
                    cross_check["warning"] = cross_warning
                cached_hithink = _cache_path(code, "nav_fyear")
                if cached_hithink.exists():
                    try:
                        meta = json.loads(cached_hithink.read_text(encoding="utf-8")).get("meta", {})
                        request_ids.append(str(meta.get("request_id", "")))
                    except (OSError, json.JSONDecodeError):
                        pass
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
                       "payment": _day(int(x["payment_date_ms"])) if x.get("payment_date_ms") else None,
                       "reinvest_date": _day(int(x["reinvestment_date_ms"])) if x.get("reinvestment_date_ms") else None} for x in raw_events["data"].get("item", []) if x.get("ex_dividend_date_ms")]
        splits = []
        if needs_full_history:
            raw_splits = _cached(code, "splits_since_inception", lambda: _akshare_fund_splits(code), force=refresh)
            splits = raw_splits.get("data", {}).get("item", [])
            request_ids.append(str(raw_splits.get("meta", {}).get("request_id", "")))
    # A same-day daily bar may still be in progress. Use completed prior days only.
    by_date: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["date"] >= today_cn().isoformat():
            continue
        values = (float(row["open"]), float(row["close"]))
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError(f"{code} 在 {row['date']} 存在无效价格/净值")
        prior = by_date.get(row["date"])
        if prior and (abs(float(prior["open"]) - values[0]) > 1e-8 or abs(float(prior["close"]) - values[1]) > 1e-8):
            raise ValueError(f"{code} 在 {row['date']} 存在冲突的重复价格/净值")
        by_date.setdefault(row["date"], row)
    rows = sorted(by_date.values(), key=lambda r: r["date"])
    if not rows:
        raise ValueError(f"{code} 无有效历史价格/净值")
    # Reject unresolved missing data within the observed interval. Chinese holidays can span ~10 days.
    for a, b in zip(rows, rows[1:]):
        if (date.fromisoformat(b["date"]) - date.fromisoformat(a["date"])).days > 16:
            raise ValueError(f"{code} 历史数据在 {a['date']} 至 {b['date']} 存在超过 16 天的缺口")
    nav_source = ("AKShare Eastmoney (成立以来单位净值、份额拆分) + Hithink 分红事件" if needs_full_history else "hithink-finance remote")
    return {"code": code, "kind": kind, "rows": rows, "events": events, "splits": splits, "cross_check": cross_check, "source": nav_source, "request_ids": [x for x in request_ids if x],
            "first": rows[0]["date"], "last": rows[-1]["date"], "price_basis": "单位净值" if kind == "fund" else "不复权开盘/收盘"}


_PERIOD_MONTHS = {"monthly": 1, "quarterly": 3, "semiannual": 6, "annual": 12}


def _planned_dates(
    start: date,
    stop: date,
    frequency: str,
    schedule_day: str = "1",
    schedule_weekday: int | None = None,
    available_dates: list[str] | None = None,
) -> list[date]:
    """Generate calendar schedule dates; daily mode uses the instrument's own data dates."""
    if frequency == "once":
        return [start]
    if frequency == "daily":
        if available_dates is None:
            return []
        return [date.fromisoformat(day) for day in available_dates if start.isoformat() <= day <= stop.isoformat()]
    if frequency in {"weekly", "biweekly"}:
        chosen_weekday = start.weekday() if schedule_weekday is None else schedule_weekday
        offset = (chosen_weekday - start.weekday()) % 7
        day = start + timedelta(days=offset)
        step = 14 if frequency == "biweekly" else 7
        result = []
        while day <= stop:
            result.append(day)
            day += timedelta(days=step)
        return result
    if frequency not in _PERIOD_MONTHS:
        raise ValueError(f"不支持的定投频率：{frequency}")

    interval = _PERIOD_MONTHS[frequency]
    month_index = start.year * 12 + start.month - 1
    stop_index = stop.year * 12 + stop.month - 1
    result = []
    while month_index <= stop_index:
        year, month0 = divmod(month_index, 12)
        month = month0 + 1
        last_day = calendar.monthrange(year, month)[1]
        day_number = last_day if schedule_day == "last" else min(int(schedule_day), last_day)
        candidate = date(year, month, day_number)
        # The start date is an inclusive first installment if the selected day has passed.
        if candidate < start:
            candidate = start
        if candidate <= stop:
            result.append(candidate)
        month_index += interval
    return result


def _period_start(start: date, planned: date, frequency: str) -> date:
    interval = _PERIOD_MONTHS[frequency]
    elapsed_months = (planned.year - start.year) * 12 + planned.month - start.month
    period_offset = (elapsed_months // interval) * interval
    month_index = start.year * 12 + start.month - 1 + period_offset
    year, month0 = divmod(month_index, 12)
    return date(year, month0 + 1, 1)


def _resolve_schedule_date(planned: date, dates: list[str], *, period_start: date | None = None) -> str:
    planned_text = planned.isoformat()
    if period_start is not None:
        index = bisect.bisect_right(dates, planned_text) - 1
        if index < 0:
            raise ValueError(f"计划日 {planned_text} 所在周期没有可用交易/净值日")
        actual = dates[index]
        if actual < period_start.isoformat():
            raise ValueError(f"计划日 {planned_text} 所在周期没有可用交易/净值日")
        if (planned - date.fromisoformat(actual)).days > 16:
            raise ValueError(f"计划日 {planned_text} 前缺少可用交易/净值日")
        return actual

    index = bisect.bisect_left(dates, planned_text)
    if index >= len(dates):
        raise ValueError(f"{planned_text} 后无可用交易/净值日")
    actual = dates[index]
    if (date.fromisoformat(actual) - planned).days > 16:
        raise ValueError(f"计划日 {planned_text} 后缺少可用交易/净值日")
    return actual


def _schedule(req: BacktestRequest) -> list[tuple[str, str, float]]:
    if req.strategy == "custom":
        return [(x.date.isoformat(), x.code, x.amount) for x in req.custom_entries]
    if req.strategy == "daily":
        return []  # Daily schedules are generated per instrument from its actual history.
    days = _planned_dates(req.start_date, req.stop_date, req.strategy, req.schedule_day, req.schedule_weekday)
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
    splits = {c: {} for c in codes}
    for c in codes:
        for split in histories[c].get("splits", []):
            splits[c].setdefault(split["date"], []).append(split)
    planned_entries = _schedule(req)
    if req.strategy == "daily":
        planned_entries = [
            (day, allocation.code, allocation.amount)
            for allocation in req.allocations
            for day in date_lists[allocation.code]
            if req.start_date.isoformat() <= day <= req.stop_date.isoformat()
        ]
    schedule = []
    for planned, code, amount in planned_entries:
        dates = date_lists[code]
        planned_day = date.fromisoformat(planned)
        last_of_period = req.schedule_day == "last" and req.strategy in _PERIOD_MONTHS
        period_start = _period_start(req.start_date, planned_day, req.strategy) if last_of_period else None
        actual = _resolve_schedule_date(planned_day, dates, period_start=period_start)
        if actual > effective_end:
            raise ValueError(f"{code} 在 {planned} 后无评估日前可用交易/净值日")
        schedule.append((actual, planned, code, amount))
    scheduled = {}
    for actual, planned, code, amount in schedule:
        scheduled.setdefault(actual, []).append((planned, code, amount, True))
    registration_days = {e["registration"] for c in codes for e in histories[c]["events"] if e.get("registration") and req.start_date.isoformat() <= e["registration"] <= effective_end}
    event_days = {d for c in codes for e in histories[c]["events"] for d in (e.get("date"), e.get("payment")) if d and req.start_date.isoformat() <= d <= effective_end}
    split_days = {d for c in codes for d in splits[c] if req.start_date.isoformat() <= d <= effective_end}
    all_days = sorted({d for c in codes for d in maps[c] if req.start_date.isoformat() <= d <= effective_end} | set(scheduled) | registration_days | event_days | split_days)
    def queue_reinvestment(day: str, code: str, amount: float):
        dates = date_lists[code]
        idx = bisect.bisect_left(dates, day)
        if idx < len(dates) and dates[idx] <= effective_end:
            scheduled.setdefault(dates[idx], []).append((day, code, amount, False))
    units = {c: 0.0 for c in codes}
    unspent = {c: 0.0 for c in codes}
    marks: dict[str, float] = {}
    cash = invested = fees = dividends = 0.0
    executed_contributions = 0
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
        for c in codes:
            for split in splits[c].get(day, []):
                factor = float(split["factor"])
                if not math.isfinite(factor) or factor <= 0:
                    raise ValueError(f"{c} 在 {day} 的份额拆分比例无效")
                before = units[c]
                units[c] *= factor
                ledger.append({"date": day, "code": c, "type": "份额拆分", "units": round(units[c] - before, 6), "note": f"按披露拆分比例 ×{factor}"})
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
            if external:
                executed_contributions += 1
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
            ledger.append({"date": day, "planned_date": planned, "code": c, "type": "买入", "price": round(price, 6), "units": round(bought, 6), "amount": round(cost, 2), "fee": round(fee, 2), "note": "按定投计划买入" if external else "分红再投资"})
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
                        "xirr": xirr, "twr_return": twr_nav - 1, "max_drawdown": max_dd, "cash": round(cash, 2), "fees": round(fees, 2), "dividends": round(dividends, 2), "actual_start": curve[0]["date"], "actual_end": actual_end,
                        "frequency": req.strategy, "schedule_day": req.schedule_day if req.strategy in _PERIOD_MONTHS else None,
                        "schedule_weekday": (req.schedule_weekday if req.schedule_weekday is not None else req.start_date.weekday()) if req.strategy in {"weekly", "biweekly"} else None,
                        "planned_entries": len(planned_entries), "executed_entries": executed_contributions},
            "coverage": [{k: h[k] for k in ("code", "source", "first", "last", "price_basis")} | {"rows": len(h["rows"]), "events": len(h["events"]), "request_ids": h.get("request_ids", [])} for h in histories.values()],
            "positions": positions, "curve": curve, "ledger": ledger, "warnings": warnings}


def calculate_long_dca(req: LongDcaRequest, history: dict[str, Any]) -> dict[str, Any]:
    """050025 NAV simulation with configurable schedule and disclosed fund dividend dates."""
    if history.get("code") != "050025" or history.get("kind") != "fund":
        raise ValueError("长期定投示例当前仅支持场外基金 050025")
    valid_rows = [r for r in history["rows"] if req.start_date.isoformat() <= r["date"] <= req.evaluation_date.isoformat()]
    if not valid_rows:
        raise ValueError("所选区间没有单位净值数据")
    valid_rows.sort(key=lambda r: r["date"])
    first_date, last_date = date.fromisoformat(valid_rows[0]["date"]), date.fromisoformat(valid_rows[-1]["date"])
    if (first_date - req.start_date).days > 16:
        raise ValueError(f"历史净值未覆盖开始日 {req.start_date}；首条可用净值为 {first_date}")
    if (req.evaluation_date - last_date).days > 16:
        raise ValueError(f"历史净值未覆盖评估日附近；末条可用净值为 {last_date}")
    end_day = last_date.isoformat()
    nav_by_day = {r["date"]: Decimal(str(r["close"])) for r in valid_rows}
    nav_days = sorted(nav_by_day)

    planned_dates = _planned_dates(
        req.start_date,
        req.stop_date,
        req.frequency,
        req.schedule_day,
        req.schedule_weekday,
        nav_days if req.frequency == "daily" else None,
    )
    schedule: dict[str, list[str]] = {}
    for planned_date in planned_dates:
        planned_day = planned_date.isoformat()
        last_of_period = req.schedule_day == "last" and req.frequency in _PERIOD_MONTHS
        period_start = _period_start(req.start_date, planned_date, req.frequency) if last_of_period else None
        actual_day = _resolve_schedule_date(planned_date, nav_days, period_start=period_start)
        schedule.setdefault(actual_day, []).append(planned_day)
    if not schedule:
        raise ValueError("评估区间内没有已执行的定投投入")

    events = []
    for source in history.get("events", []):
        day = source.get("date")
        if not day or day < req.start_date.isoformat() or day > end_day:
            continue
        events.append({
            "date": day,
            "registration": source.get("registration"),
            "payment": source.get("payment"),
            "reinvest_date": source.get("reinvest_date"),
            "cash_per_unit": Decimal(str(source.get("cash") or 0)),
        })
    splits_by_day: dict[str, list[Decimal]] = {}
    for split in history.get("splits", []):
        day = split.get("date")
        if day and req.start_date.isoformat() <= day <= end_day:
            factor = Decimal(str(split.get("factor", "0")))
            if not factor.is_finite() or factor <= 0:
                raise ValueError(f"基金份额拆分比例无效：{day}")
            splits_by_day.setdefault(day, []).append(factor)
    event_days = ({d for e in events for d in (e["registration"], e["date"], e["payment"], e["reinvest_date"]) if d and req.start_date.isoformat() <= d <= end_day}
                  | set(splits_by_day))
    all_days = sorted(({d for d in nav_days if req.start_date.isoformat() <= d <= end_day}) | set(schedule) | event_days)

    units = Decimal("0")
    cash = Decimal("0")
    invested = Decimal("0")
    fees = Decimal("0")
    dividends = Decimal("0")
    pending: list[dict[str, Any]] = []
    entitlement: dict[int, Decimal] = {}
    reinvest_queue: dict[str, list[Decimal]] = {}
    cash_queue: dict[str, list[Decimal]] = {}
    curve = []
    ledger = []
    flows: list[tuple[str, float]] = []
    executed = 0

    def next_nav_day(day: str) -> str | None:
        idx = bisect.bisect_left(nav_days, day)
        return nav_days[idx] if idx < len(nav_days) and nav_days[idx] <= end_day else None

    for day in all_days:
        for factor in splits_by_day.get(day, []):
            before = units
            units *= factor
            ledger.append({"date": day, "code": "050025", "type": "份额拆分", "units": float(units - before), "note": f"按披露拆分比例 ×{factor}"})
        # Registration-day holdings determine each fund dividend entitlement.
        for i, event in enumerate(events):
            if event["registration"] == day:
                entitlement[i] = units
        # A dividend becomes an asset on ex-date; it is reinvested or paid on its disclosed date.
        for i, event in enumerate(events):
            if event["date"] == day:
                amount = entitlement.get(i, units) * event["cash_per_unit"]
                if amount > 0:
                    action_date = (event["reinvest_date"] if req.reinvest_dividends else event["payment"]) or event["payment"] or day
                    pending.append({"amount": amount, "action_date": action_date, "reinvest": req.reinvest_dividends})
                    dividends += amount
                    ledger.append({"date": day, "code": "050025", "type": "分红确认", "amount": round(float(amount), 2), "note": "金额按登记日持有份额及每份税后分红计算"})
        for item in list(pending):
            if item["action_date"] == day:
                target = next_nav_day(day) if item["reinvest"] else day
                if target is None:
                    continue
                queue = reinvest_queue if item["reinvest"] else cash_queue
                queue.setdefault(target, []).append(item["amount"])
                pending.remove(item)
        if day in cash_queue:
            paid = sum(cash_queue.pop(day), Decimal("0"))
            cash += paid
            ledger.append({"date": day, "code": "050025", "type": "现金分红到账", "amount": round(float(paid), 2), "note": "按披露支付日入账"})
        if day not in nav_by_day:
            continue
        nav = nav_by_day[day]
        if day in reinvest_queue:
            for amount in reinvest_queue.pop(day):
                bought = (amount / nav).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
                cost = bought * nav
                units += bought
                cash += amount - cost
                ledger.append({"date": day, "code": "050025", "type": "分红再投资", "price": float(nav), "units": float(bought), "amount": round(float(cost), 2), "fee": 0, "note": "按披露再投资日净值；未扣普通申购费"})
        for planned_day in schedule.get(day, []):
            gross = req.monthly_amount
            fee = gross - gross / (Decimal("1") + req.fee_rate)
            net = gross - fee
            bought = (net / nav).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            cost = bought * nav
            cash += gross - fee - cost
            units += bought
            invested += gross
            fees += fee
            flows.append((day, -float(gross)))
            executed += 1
            ledger.append({"date": day, "planned_date": planned_day, "code": "050025", "type": "定投申购", "price": float(nav), "units": float(bought), "amount": round(float(gross), 2), "fee": round(float(fee), 2), "note": "场外基金按当日单位净值；份额保留两位小数"})

        receivable = sum((item["amount"] for item in pending), Decimal("0"))
        value = cash + receivable + units * nav
        curve.append({"date": day, "value": round(float(value), 2), "invested": round(float(invested), 2), "cash": round(float(cash), 2)})

    if not curve or invested <= 0:
        raise ValueError("所选区间没有可计算的已执行定投")
    actual_end = curve[-1]["date"]
    final_value = Decimal(str(curve[-1]["value"]))
    flows.append((actual_end, float(final_value)))
    irr = None if len({day for day, _ in flows}) < 2 else _xirr(flows)
    digest_payload = json.dumps({"rows": valid_rows, "events": events, "splits": splits_by_day}, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    summary = {
        "invested": round(float(invested), 2),
        "value": round(float(final_value), 2),
        "profit": round(float(final_value - invested), 2),
        "simple_return": float(final_value / invested - 1) if invested else None,
        "xirr": irr,
        "twr_return": None,
        "max_drawdown": None,
        "cash": round(float(cash), 2),
        "fees": round(float(fees), 2),
        "dividends": round(float(dividends), 2),
        "actual_start": curve[0]["date"],
        "actual_end": actual_end,
        "first_investment_date": flows[0][0],
        "requested_evaluation_date": req.evaluation_date.isoformat(),
        "planned_installments": len([d for d in planned_dates if d <= req.evaluation_date]),
        "executed_installments": executed,
        "frequency": req.frequency,
        "schedule_day": req.schedule_day if req.frequency in _PERIOD_MONTHS else None,
        "schedule_weekday": req.schedule_weekday if req.frequency in {"weekly", "biweekly"} else None,
        "units": round(float(units), 2),
        "latest_nav": float(nav_by_day[actual_end]),
        "pending_dividends": round(float(sum((item["amount"] for item in pending), Decimal("0"))), 2),
    }
    warnings = [
        "假设计划日期均可申购；未还原历史限购、暂停申购及平台额度。",
        f"申购费率按当前模拟值 {float(req.fee_rate) * 100:.4g}% 统一应用整个历史区间；非历史逐年费率。",
        "评估日按最新完整单位净值日截断；累计收益率按累计投入计算，年化为资金加权 XIRR。",
        "净值包含基金层面的费用；期末总值按持有份额和分红应收计，不扣未发生的赎回费用。",
        "定投计划日按所选规则生成，遇非净值日按页面说明顺延或回退到本期最后净值日。",
    ]
    cross_check = history.get("cross_check") or {}
    if cross_check.get("warning"):
        warnings.insert(0, cross_check["warning"])
    return {
        "mode": "long_dca",
        "calculation_version": "long-dca-v2",
        "data_snapshot_sha256": hashlib.sha256(digest_payload).hexdigest(),
        "summary": summary,
        "coverage": [{"code": "050025", "source": history.get("source", "unknown"), "first": history["first"], "last": history["last"], "rows": len(history["rows"]), "events": len(history.get("events", [])), "splits": len(history.get("splits", [])), "cross_check": cross_check, "request_ids": history.get("request_ids", []), "price_basis": "单位净值"}],
        "positions": [{"code": "050025", "name": "博时标普500ETF联接A", "units": round(float(units), 2), "last_price": float(nav_by_day[actual_end]), "market_value": round(float(units * nav_by_day[actual_end]), 2), "pending_budget": round(float(cash), 2)}],
        "curve": curve,
        "ledger": ledger,
        "warnings": warnings,
    }


def init_runs(path: Path = RUN_DB):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, created_at TEXT NOT NULL, status TEXT NOT NULL, request_json TEXT NOT NULL, result_json TEXT, error TEXT)")


def create_run(req: BacktestRequest | LongDcaRequest, path: Path = RUN_DB) -> str:
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


def execute_long_dca_run(run_id: str, req: LongDcaRequest, path: Path = RUN_DB):
    try:
        history = load_history("050025", req.start_date, req.evaluation_date, refresh=req.refresh_data)
        result = calculate_long_dca(req, history)
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
