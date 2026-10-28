from __future__ import annotations

import csv
import io
import json
import os
import shutil
import sqlite3
import subprocess
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

try:
    from .backtest import BacktestRequest, LongDcaRequest
except ImportError:
    from backtest import BacktestRequest, LongDcaRequest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA_DIR = HERE / "runtime"
DB_PATH = Path(os.environ.get("PERSONAL_SIM_DB", DATA_DIR / "simulator.sqlite3"))
CN_TZ = ZoneInfo("Asia/Shanghai")
STALE_SECONDS = 60
D = Decimal

INSTRUMENTS: dict[str, dict[str, Any]] = {
    "017641": {"name": "摩根标普500指数(QDII)人民币A", "kind": "fund", "bucket": "sp500", "asset": "equity", "daily_cap": "10", "fee": "0.0012", "fee_label": "档案折后费率；需核验本人渠道", "platform_verified": False},
    "050025": {"name": "博时标普500ETF联接A", "kind": "fund", "bucket": "sp500", "asset": "equity", "suspended": True, "fee": "0.0012", "fee_label": "档案折后费率；需核验本人渠道", "platform_verified": False},
    "012692": {"name": "博时中债0-3年国开行债券ETF联接A", "kind": "fund", "bucket": "bond", "asset": "bond", "fee": "0.0005", "fee_label": "档案折后费率；需核验本人渠道", "platform_verified": False},
    "513800": {"name": "南方标普日本TOPIX ETF", "kind": "etf", "bucket": "topix", "asset": "equity", "lot": 100, "tick": "0.001"},
    "601398": {"name": "工商银行", "kind": "stock", "bucket": "stock", "asset": "equity", "lot": 100, "industry": "银行", "research_approved": False},
    "600900": {"name": "长江电力", "kind": "stock", "bucket": "stock", "asset": "equity", "lot": 100, "industry": "公用事业", "research_approved": False},
    "000333": {"name": "美的集团", "kind": "stock", "bucket": "stock", "asset": "equity", "lot": 100, "industry": "家电", "research_approved": False},
    "600690": {"name": "海尔智家", "kind": "stock", "bucket": "stock", "asset": "equity", "lot": 100, "industry": "家电", "research_approved": False},
    "600660": {"name": "福耀玻璃", "kind": "stock", "bucket": "stock", "asset": "equity", "lot": 100, "industry": "汽车零部件", "research_approved": False},
}

LONG_TERM_TARGETS = {"sp500": 0.18, "topix": 0.12, "stock": 0.05, "bond": 0.45, "cash": 0.20}
CAPS = {"sp500": 0.25, "topix": 0.20, "stock": 0.05, "equity": 0.45, "bond": 0.50}
MONTHLY_BUDGET = {"sp500": 540, "topix": 360, "stock_reserve": 150, "bond": 1350, "cash_reserve": 600}


def now_iso() -> str:
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def as_decimal(value: Any, field: str) -> Decimal:
    try:
        out = D(str(value))
    except Exception as exc:
        raise HTTPException(400, f"{field} 必须是有效数字") from exc
    if not out.is_finite():
        raise HTTPException(400, f"{field} 必须是有限数字")
    return out


def money(value: Decimal) -> Decimal:
    return value.quantize(D("0.01"), rounding=ROUND_HALF_UP)


def parse_time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=CN_TZ)
        return result.astimezone(timezone.utc)
    except Exception as exc:
        raise HTTPException(400, "时间必须使用 ISO 格式，例如 2026-10-27T10:00:00+08:00") from exc


def cli_base_command() -> list[str]:
    configured = os.environ.get("HITHINK_CLI")
    if configured:
        path = Path(configured)
        if os.name == "nt" and path.suffix.lower() in (".ps1", ".cmd", ".bat"):
            return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)]
        return [configured]
    if os.name == "nt":
        appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        ps1 = appdata / "npm" / "hithink-finance.ps1"
        if ps1.exists():
            return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)]
        cmd = appdata / "npm" / "hithink-finance.cmd"
        if cmd.exists():
            return ["cmd.exe", "/d", "/c", str(cmd)]
    return [shutil.which("hithink-finance") or "hithink-finance"]


@contextmanager
def db_conn(path: Path):
    with closing(sqlite3.connect(path, timeout=10)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn


def read_envelope(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not data.get("ok"):
            return None
        return data
    except (OSError, json.JSONDecodeError):
        return None


def init_db(path: Path, seed: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with db_conn(path) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS account (
          id INTEGER PRIMARY KEY CHECK(id=1), cash TEXT NOT NULL DEFAULT '0', units TEXT NOT NULL DEFAULT '0',
          peak_unit_nav TEXT NOT NULL DEFAULT '1', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS holdings (
          code TEXT PRIMARY KEY, quantity TEXT NOT NULL, avg_cost TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS quotes (
          code TEXT PRIMARY KEY, price TEXT NOT NULL, observed_at TEXT NOT NULL, source TEXT NOT NULL,
          request_id TEXT, raw_json TEXT NOT NULL, iopv TEXT, iopv_at TEXT, iopv_source TEXT
        );
        CREATE TABLE IF NOT EXISTS flows (
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, amount TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS orders (
          id TEXT PRIMARY KEY, code TEXT NOT NULL, side TEXT NOT NULL, status TEXT NOT NULL,
          quantity TEXT NOT NULL, amount TEXT NOT NULL, limit_price TEXT, fee TEXT NOT NULL,
          fee_rate TEXT NOT NULL, premium_pct TEXT, nav TEXT, units TEXT, reserved TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, source_json TEXT NOT NULL, error TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (
          id INTEGER PRIMARY KEY CHECK(id=1), value_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS nav_points (
          id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, nav TEXT NOT NULL, units TEXT NOT NULL,
          unit_nav TEXT NOT NULL, drawdown TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(created_at, code, status);
        """)
        conn.execute("INSERT OR IGNORE INTO account(id,cash,units,peak_unit_nav,created_at) VALUES(1,'0','0','1',?)", (now_iso(),))
        conn.execute("INSERT OR IGNORE INTO settings(id,value_json) VALUES(1,?)", (json.dumps({
            "commission_rate": "0.0003", "minimum_commission": "5", "stamp_duty_rate": "",
            "stamp_duty_verified": False, "quota_017641": "10", "platform_017641_verified": False,
            "platform_012692_verified": False, "fee_017641": "0.0012", "fee_012692": "0.0005",
            "last_manual_price_note": "券商佣金、平台费率及限额需在本人平台复核后更新。",
        }, ensure_ascii=False),))
    if seed:
        seed_quotes(path)


def seed_quotes(path: Path) -> None:
    sources = [
        ("513800", ROOT / "investment_assist_2026-10-27" / "snapshot_513800_2026-10-27.json", " fund snapshot · historical seed"),
        ("__stocks__", ROOT / "investment_assist_2026-10-27" / "snapshot_stocks_2026-10-27.json", " market snapshot · historical seed"),
    ]
    with db_conn(path) as conn:
        for special, src, note in sources:
            envelope = read_envelope(src)
            if not envelope:
                continue
            request_id = envelope.get("meta", {}).get("request_id")
            stamp = envelope.get("data", {}).get("timestamp")
            observed = datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat(timespec="seconds") if stamp else now_iso()
            items = envelope.get("data", {}).get("item", [])
            for item in items:
                code = item.get("ticker") or item.get("thscode", "").split(".")[0]
                if code not in INSTRUMENTS or not item.get("last_price"):
                    continue
                conn.execute("INSERT OR IGNORE INTO quotes(code,price,observed_at,source,request_id,raw_json) VALUES(?,?,?,?,?,?)", (
                    code, str(item["last_price"]), observed, f"hithink-finance{note}", request_id, json.dumps(item, ensure_ascii=False)))
        profiles = {
            "017641": ROOT / "investment_assist_2026-10-27" / "profile_017641.json",
            "012692": ROOT / "investment_assist_2026-10-27" / "profile_012692.json",
        }
        for code, src in profiles.items():
            env = read_envelope(src)
            items = env.get("data", {}).get("item", []) if env else []
            if not items:
                continue
            p = items[0]
            # The profile endpoint does not supply a NAV date: do not use its unit_nav as an executable quote.
            conn.execute("INSERT OR IGNORE INTO quotes(code,price,observed_at,source,request_id,raw_json) VALUES(?,?,?,?,?,?)", (
                code, str(p.get("unit_nav", "0")), "", "hithink fund.profile (NAV date unknown; display only)", env.get("meta", {}).get("request_id"), json.dumps(p, ensure_ascii=False)))


class DepositIn(BaseModel):
    amount: Decimal = Field(gt=0, le=100000000)
    note: str = "模拟月度投入"


class RouteIn(BaseModel):
    amount: Decimal = Field(default=Decimal("3000"), gt=0, le=100000000)


class QuoteIn(BaseModel):
    code: str
    price: Decimal = Field(gt=0)
    observed_at: str
    source_note: str = "本人券商页面录入"


class IopvIn(BaseModel):
    code: str = "513800"
    iopv: Decimal = Field(gt=0)
    observed_at: str
    source_note: str = "本人券商页面录入"


class SettingsIn(BaseModel):
    commission_rate: Decimal | None = Field(default=None, ge=0, le=0.1)
    minimum_commission: Decimal | None = Field(default=None, ge=0, le=10000)
    stamp_duty_rate: Decimal | None = Field(default=None, ge=0, le=0.1)
    stamp_duty_verified: bool | None = None
    platform_017641_verified: bool | None = None
    platform_012692_verified: bool | None = None
    quota_017641: Decimal | None = Field(default=None, ge=0, le=1000000)
    fee_017641: Decimal | None = Field(default=None, ge=0, le=0.1)
    fee_012692: Decimal | None = Field(default=None, ge=0, le=0.1)


class PreviewIn(BaseModel):
    code: str
    side: str = "buy"
    quantity: Decimal | None = Field(default=None, gt=0)
    amount: Decimal | None = Field(default=None, gt=0)
    limit_price: Decimal | None = Field(default=None, gt=0)
    idempotency_key: str | None = None


def read_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT value_json FROM settings WHERE id=1").fetchone()
    return json.loads(row[0])


def instrument_info(code: str) -> dict[str, Any]:
    if code not in INSTRUMENTS:
        raise HTTPException(400, "该标的未纳入模拟器候选清单")
    return INSTRUMENTS[code]


def quote_row(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM quotes WHERE code=?", (code,)).fetchone()


def is_fresh(quote: sqlite3.Row | None, seconds: int = STALE_SECONDS) -> bool:
    if not quote or not quote["observed_at"]:
        return False
    age = (datetime.now(timezone.utc) - parse_time(quote["observed_at"])).total_seconds()
    return -2 <= age <= seconds


def held_quantity(conn: sqlite3.Connection, code: str) -> Decimal:
    row = conn.execute("SELECT quantity FROM holdings WHERE code=?", (code,)).fetchone()
    return D(row[0]) if row else D("0")


def reserved_cash(conn: sqlite3.Connection) -> Decimal:
    rows = conn.execute("SELECT reserved FROM orders WHERE status IN ('pending_fill','pending_confirmation')").fetchall()
    return sum((D(r[0]) for r in rows), D("0"))


def account_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    account = conn.execute("SELECT * FROM account WHERE id=1").fetchone()
    cash = D(account["cash"])
    reserved = reserved_cash(conn)
    positions: list[dict[str, Any]] = []
    bucket_values = {key: D("0") for key in ("sp500", "topix", "stock", "bond")}
    for h in conn.execute("SELECT * FROM holdings").fetchall():
        info = INSTRUMENTS.get(h["code"], {})
        q = quote_row(conn, h["code"])
        px = D(q["price"]) if q and q["observed_at"] else D(h["avg_cost"])
        qty = D(h["quantity"])
        value = qty * px
        bucket = info.get("bucket", "stock")
        if bucket in bucket_values:
            bucket_values[bucket] += value
        positions.append({"code": h["code"], "name": info.get("name", h["code"]), "quantity": str(qty), "avg_cost": str(D(h["avg_cost"])), "price": str(px), "market_value": str(money(value)), "price_fresh": is_fresh(q)})
    # Reserve pending buy amounts into their corresponding exposure bucket.
    pending_by_bucket = {key: D("0") for key in bucket_values}
    for order in conn.execute("SELECT code,amount,status FROM orders WHERE status IN ('pending_fill','pending_confirmation')").fetchall():
        info = INSTRUMENTS.get(order["code"], {})
        bucket = info.get("bucket")
        if bucket in pending_by_bucket:
            pending_by_bucket[bucket] += D(order["amount"])
    exposure = {key: bucket_values[key] + pending_by_bucket[key] for key in bucket_values}
    nav = cash + reserved + sum(bucket_values.values(), D("0"))
    units = D(account["units"])
    unit_nav = nav / units if units > 0 else D("1")
    peak = max(D(account["peak_unit_nav"]), unit_nav)
    drawdown = (unit_nav / peak - 1) if peak > 0 else D("0")
    conn.execute("UPDATE account SET peak_unit_nav=? WHERE id=1", (str(peak),))
    ratios = {key: float(value / nav) if nav > 0 else 0.0 for key, value in exposure.items()}
    ratios["cash"] = float((cash + reserved) / nav) if nav > 0 else 0.0
    return {"cash": money(cash), "reserved_cash": money(reserved), "nav": money(nav), "units": units, "unit_nav": unit_nav, "drawdown": drawdown, "peak_unit_nav": peak, "bucket_values": exposure, "ratios": ratios, "positions": positions}


def record_nav(conn: sqlite3.Connection) -> None:
    metrics = account_metrics(conn)
    conn.execute("INSERT INTO nav_points(at,nav,units,unit_nav,drawdown) VALUES(?,?,?,?,?)", (
        now_iso(), str(metrics["nav"]), str(metrics["units"]), str(metrics["unit_nav"]), str(metrics["drawdown"])))


def preview_order(conn: sqlite3.Connection, payload: PreviewIn) -> dict[str, Any]:
    info = instrument_info(payload.code)
    side = payload.side.lower()
    if side not in ("buy", "sell"):
        raise HTTPException(400, "仅支持买入或卖出")
    if info.get("suspended") and side == "buy":
        return {"ok": False, "reasons": ["050025 当前标记为暂停申购"], "code": payload.code}
    if info["kind"] == "stock" and side == "buy" and not info.get("research_approved"):
        return {"ok": False, "reasons": ["该公司仍是重点跟踪对象；研究卡和保守估值未获买入批准"], "code": payload.code}
    settings = read_settings(conn)
    reasons: list[str] = []
    quote = quote_row(conn, payload.code)
    if info["kind"] != "fund":
        if not quote or not quote["observed_at"]:
            reasons.append("缺少带日期的有效价格；先刷新行情或录入券商端价格")
        elif not is_fresh(quote):
            reasons.append("参考价格已超过 60 秒，先刷新或录入当前券商报价")

    if info["kind"] == "fund":
        if payload.amount is None:
            reasons.append("场外基金申购请填写金额")
        if payload.quantity is not None:
            reasons.append("场外基金使用申购金额，不填写份额")
        verified = settings.get(f"platform_{payload.code}_verified", False)
        if not verified:
            reasons.append("尚未确认本人平台的开放状态、限额与实际折扣费率")
        if payload.code == "017641":
            quota = D(settings.get("quota_017641", "10"))
            today = datetime.now(CN_TZ).date().isoformat()
            used = D("0")
            for row in conn.execute("SELECT amount,created_at FROM orders WHERE code='017641' AND side='buy' AND status IN ('pending_confirmation','filled')").fetchall():
                if row["created_at"].startswith(today):
                    used += D(row["amount"])
            if payload.amount is not None and used + payload.amount > quota:
                reasons.append(f"今日累计申购将超过 ¥{quota} 限额（已提交/成交 ¥{used}）")
        fee_rate = D(settings.get(f"fee_{payload.code}", info.get("fee", "0")))
        amount = payload.amount or D("0")
        fee = amount - amount / (1 + fee_rate) if amount > 0 else D("0")
        quantity = D("0")
        limit_price = None
        if amount <= 0:
            reasons.append("申购金额必须大于零")
        cost_ratio = fee / amount if amount > 0 else D("0")
        if amount > 0 and cost_ratio > D("0.005"):
            reasons.append("申购费占比超过 0.5% 成本线")
        if info["asset"] == "equity" and account_metrics(conn)["drawdown"] <= D("-0.20"):
            reasons.append("账户回撤达到 20%，暂停新增权益")
        order_state = "pending_confirmation"
        reserved = amount
        premium = None
    else:
        if payload.quantity is None:
            reasons.append("场内交易请填写份额/股数")
        if payload.amount is not None:
            reasons.append("场内交易填写数量，不填写申购金额")
        qty = payload.quantity or D("0")
        lot = D(str(info.get("lot", 100)))
        if qty > 0 and qty % lot != 0:
            reasons.append(f"数量必须是 {lot} 的整数倍")
        px = as_decimal(quote["price"], "参考价格") if quote and quote["price"] else D("0")
        limit_price = payload.limit_price or px
        if payload.limit_price is None:
            reasons.append("请填写限价；参考价不会自动作为委托价")
        tick = D(str(info.get("tick", "0.01")))
        if limit_price > 0 and (limit_price / tick) != (limit_price / tick).to_integral_value():
            reasons.append(f"委托价必须符合最小变动单位 {tick}")
        if info["kind"] == "etf" and side == "buy":
            if not quote or not quote["iopv"] or not quote["iopv_at"]:
                reasons.append("513800 缺少同一时点 IOPV；不能判断溢价")
                premium = None
            else:
                delta = abs((parse_time(quote["observed_at"]) - parse_time(quote["iopv_at"])).total_seconds())
                if delta > STALE_SECONDS:
                    reasons.append("场内价格与 IOPV 时间相差超过 60 秒")
                iopv = D(quote["iopv"])
                premium = (px / iopv - 1) * 100 if iopv > 0 else D("999")
                if premium > D("2"):
                    reasons.append(f"溢价 {premium:.3f}% 超过 2% 上限")
        else:
            premium = None
        notional = qty * (limit_price if limit_price > 0 else px)
        fee_rate = D(settings["commission_rate"])
        fee = max(notional * fee_rate, D(settings["minimum_commission"])) if notional > 0 else D("0")
        if side == "buy" and notional > 0 and fee / notional > D("0.005"):
            reasons.append(f"显性费用约 {(fee / notional * 100):.2f}%，超过 0.5% 成本线")
        if side == "sell" and info["kind"] == "stock" and not settings.get("stamp_duty_verified"):
            reasons.append("股票卖出印花税/平台费用尚未按交易日核实")
        pending_sell = sum((D(row[0]) for row in conn.execute(
            "SELECT quantity FROM orders WHERE code=? AND side='sell' AND status='pending_fill'", (payload.code,)).fetchall()), D("0"))
        if side == "sell" and qty > held_quantity(conn, payload.code) - pending_sell:
            reasons.append("卖出数量超过模拟持仓")
        if side == "buy" and info["asset"] == "equity" and account_metrics(conn)["drawdown"] <= D("-0.20"):
            reasons.append("账户回撤达到 20%，暂停新增权益")
        amount = notional
        quantity = qty
        reserved = notional + fee if side == "buy" else D("0")
        order_state = "pending_fill"

    metrics = account_metrics(conn)
    if side == "buy" and reserved > metrics["cash"]:
        reasons.append(f"可用现金不足：需要约 ¥{money(reserved)}，当前 ¥{metrics['cash']}")
    if side == "buy" and metrics["nav"] > 0 and not reasons:
        current = metrics["bucket_values"]
        after_nav = metrics["nav"] - fee
        addition = amount - fee if info["kind"] == "fund" else amount
        bucket = info["bucket"]
        cap_key = bucket if bucket in ("sp500", "topix", "stock", "bond") else None
        if cap_key and after_nav > 0 and (D(str(current[cap_key])) + addition) / after_nav > D(str(CAPS[cap_key])):
            reasons.append(f"交易后 {cap_key} 超过 {CAPS[cap_key] * 100:.0f}% 临时上限")
        if info["asset"] == "equity" and after_nav > 0:
            equity_now = sum((D(str(current[k])) for k in ("sp500", "topix", "stock")), D("0"))
            if (equity_now + addition) / after_nav > D(str(CAPS["equity"])):
                reasons.append("交易后权益合计超过 45% 上限")
        if bucket == "bond" and after_nav > 0 and (D(str(current["bond"])) + addition) / after_nav > D(str(CAPS["bond"])):
            reasons.append("交易后短债超过 50% 临时上限")
    return {"ok": not reasons, "reasons": reasons, "code": payload.code, "name": info["name"], "side": side,
            "kind": info["kind"], "quantity": str(quantity), "amount": str(money(amount)), "limit_price": str(limit_price) if limit_price is not None else None,
            "fee": str(money(fee)), "fee_rate": str(fee_rate), "reserved": str(money(reserved)),
            "premium_pct": str(premium.quantize(D("0.001"), rounding=ROUND_HALF_UP)) if premium is not None else None,
            "status_after_submit": order_state, "quote_source": quote["source"] if quote else None,
            "quote_observed_at": quote["observed_at"] if quote else None}


def calculate_route(conn: sqlite3.Connection, contribution: Decimal) -> dict[str, Any]:
    """Preview one monthly contribution without booking money or orders."""
    settings = read_settings(conn)
    metrics = account_metrics(conn)
    projected_nav = D(str(metrics["nav"])) + contribution
    current = metrics["bucket_values"]
    remaining = contribution
    allocations: dict[str, Decimal] = {"sp500": D("0"), "topix": D("0"), "bond": D("0")}
    reasons: dict[str, str] = {}
    reserved_stock = D("150")
    cash_floor = D("600")
    spendable = max(D("0"), remaining - reserved_stock - cash_floor)

    # Exact S&P 500 alternate first; 050025 is suspended and is never routed.
    sp_room = max(D("0"), projected_nav * D("0.25") - D(str(current["sp500"])))
    today = datetime.now(CN_TZ).date().isoformat()
    quota_used = sum((D(row[0]) for row in conn.execute(
        "SELECT amount FROM orders WHERE code='017641' AND side='buy' AND status IN ('pending_confirmation','filled') AND created_at LIKE ?", (today + "%",)).fetchall()), D("0"))
    quota_left = max(D("0"), D(settings["quota_017641"]) - quota_used)
    equity_paused = D(str(metrics["drawdown"])) <= D("-0.20")
    if equity_paused:
        reasons["sp500"] = "回撤达到 20%，暂停新增权益"
    elif not settings.get("platform_017641_verified"):
        reasons["sp500"] = "本人平台申购状态、日限额与费率待核验"
    else:
        allocations["sp500"] = min(D("540"), sp_room, quota_left, spendable)
        spendable -= allocations["sp500"]
        reasons["sp500"] = "017641 同指数替代；受日限额和 25% 上限约束" if allocations["sp500"] else "日限额或仓位余量为零"

    # TOPIX allocation requires fresh exchange quote, contemporaneous IOPV and an affordable whole lot.
    topix_quote = quote_row(conn, "513800")
    topix_investment = D("0")
    if equity_paused:
        reasons["topix"] = "回撤达到 20%，暂停新增权益"
    elif not topix_quote or not is_fresh(topix_quote):
        reasons["topix"] = "缺少当前 60 秒内的场内报价"
    elif not topix_quote["iopv"] or not topix_quote["iopv_at"] or abs((parse_time(topix_quote["observed_at"]) - parse_time(topix_quote["iopv_at"])).total_seconds()) > STALE_SECONDS:
        reasons["topix"] = "缺少同一时点 IOPV"
    else:
        px, iopv = D(topix_quote["price"]), D(topix_quote["iopv"])
        premium = (px / iopv - 1) * 100
        topix_room = max(D("0"), projected_nav * D("0.20") - D(str(current["topix"])))
        equity_room = max(D("0"), projected_nav * D("0.45") - sum((D(str(current[k])) for k in ("sp500", "topix", "stock")), D("0")) - allocations["sp500"])
        cap_amount = min(D("360"), topix_room, equity_room, spendable)
        qty = ((cap_amount / px / D("100")).to_integral_value(rounding=ROUND_DOWN) * D("100")) if px > 0 else D("0")
        notional = qty * px
        commission = max(notional * D(settings["commission_rate"]), D(settings["minimum_commission"])) if notional else D("0")
        total = notional + commission
        if premium > D("2"):
            reasons["topix"] = f"溢价 {premium:.3f}% 超过 2%"
        elif qty <= 0 or total > cap_amount:
            reasons["topix"] = "整手与最低佣金使本期预算不满足 0.5% 成本线"
        elif commission / notional > D("0.005"):
            reasons["topix"] = f"最低佣金占成交额 {commission / notional * 100:.2f}%，超过 0.5%"
        else:
            topix_investment = total
            allocations["topix"] = total
            spendable -= total
            reasons["topix"] = f"通过预检，可买 {qty} 份；需下单前再次确认数据"

    # Base bond budget, then unspent overseas budget to the 50% temporary cap.
    bond_verified = bool(settings.get("platform_012692_verified"))
    bond_room = max(D("0"), projected_nav * D("0.50") - D(str(current["bond"])))
    bond_rate = D(settings["fee_012692"])
    bond_cost_ratio = bond_rate / (1 + bond_rate)
    bond_base = D("0")
    if bond_verified and bond_cost_ratio <= D("0.005"):
        bond_base = min(D("1350"), bond_room, spendable)
        allocations["bond"] = bond_base
        spendable -= bond_base
        reasons["bond"] = "渠道已核验；先按 ¥1,350 基础预算，受 50% 上限约束"
    else:
        reasons["bond"] = "本人平台开放状态/实际申购费率待核验" if not bond_verified else "申购费用超过 0.5% 成本线"

    overseas_unspent = max(D("0"), D("900") - allocations["sp500"] - topix_investment)
    if bond_verified and allocations["bond"] > 0 and bond_cost_ratio <= D("0.005"):
        extra = min(overseas_unspent, max(D("0"), bond_room - allocations["bond"]), spendable)
        allocations["bond"] += extra
        spendable -= extra
    else:
        extra = D("0")
    cash_actual = max(D("0"), contribution - reserved_stock - sum(allocations.values(), D("0")))
    return {
        "contribution": str(money(contribution)), "projected_nav": str(money(projected_nav)),
        "allocations": {k: str(money(v)) for k, v in allocations.items()},
        "stock_reserve": str(money(reserved_stock)), "cash": str(money(cash_actual)),
        "bond_topup_from_unspent_overseas": str(money(extra)),
        "overseas_unspent_after_route": str(money(max(D("0"), overseas_unspent - extra))),
        "reasons": reasons,
        "total": str(money(sum(allocations.values(), D("0")) + reserved_stock + cash_actual)),
        "note": "预算预览不代表账户已入金或已成交；实际委托仍须逐笔预检。",
    }


def create_app(db_path: Path = DB_PATH, seed: bool = True) -> FastAPI:
    try:
        from . import backtest as bt
    except ImportError:
        import backtest as bt
    init_db(db_path, seed=seed)
    run_db = db_path.parent / "backtests.sqlite3"
    bt.init_runs(run_db)
    app = FastAPI(title="个人投资模拟器", version="1.0.0")
    app.state.db_path = db_path
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(HERE / "static" / "backtest.html")

    @app.get("/trade")
    def trade_page() -> FileResponse:
        return FileResponse(HERE / "static" / "index.html")

    @app.get("/settings")
    def settings_page() -> FileResponse:
        return FileResponse(HERE / "static" / "settings.html")

    @app.get("/api/backtest/instruments")
    def backtest_instruments() -> dict[str, Any]:
        items = []
        for code, info in INSTRUMENTS.items():
            items.append({"code": code, "name": info["name"], "kind": info["kind"], "bucket": info["bucket"]})
        return {"items": items, "source": "hithink-finance 远端历史接口；实际覆盖区间以回测结果为准"}

    @app.post("/api/backtests")
    def start_backtest(request: BacktestRequest, background: BackgroundTasks) -> dict[str, str]:
        run_id = bt.create_run(request, run_db)
        background.add_task(bt.execute_run, run_id, request, run_db)
        return {"id": run_id, "status": "running"}

    @app.post("/api/backtests/2016-dca")
    def start_2016_dca(request: LongDcaRequest, background: BackgroundTasks) -> dict[str, str]:
        run_id = bt.create_run(request, run_db)
        background.add_task(bt.execute_long_dca_run, run_id, request, run_db)
        return {"id": run_id, "status": "running"}

    @app.get("/api/backtests")
    def backtest_runs() -> dict[str, Any]:
        with closing(sqlite3.connect(run_db)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT id,created_at,status,error FROM runs ORDER BY created_at DESC LIMIT 30").fetchall()
        return {"items": [dict(x) for x in rows]}

    @app.get("/api/backtests/{run_id}")
    def backtest_run(run_id: str) -> dict[str, Any]:
        return bt.get_run(run_id, run_db)

    @app.get("/api/backtests/{run_id}/export")
    def export_backtest(run_id: str) -> JSONResponse:
        record = bt.get_run(run_id, run_db)
        return JSONResponse(record, headers={"Content-Disposition": f'attachment; filename="backtest-{run_id}.json"'})

    @app.get("/api/backtests/{run_id}/ledger.csv")
    def export_backtest_ledger(run_id: str) -> Response:
        record = bt.get_run(run_id, run_db)
        result = record.get("result") or {}
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["date", "planned_date", "code", "type", "price", "units", "amount", "fee", "note"])
        for row in result.get("ledger", []):
            writer.writerow([row.get(key, "") for key in ("date", "planned_date", "code", "type", "price", "units", "amount", "fee", "note")])
        return Response("\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="backtest-{run_id}-ledger.csv"'})

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        with db_conn(db_path) as conn:
            metrics = account_metrics(conn)
            quotes = []
            for row in conn.execute("SELECT * FROM quotes ORDER BY code").fetchall():
                info = INSTRUMENTS.get(row["code"], {})
                if not info:
                    continue
                quotes.append({"code": row["code"], "name": info["name"], "price": row["price"], "observed_at": row["observed_at"],
                              "source": row["source"], "request_id": row["request_id"], "fresh": is_fresh(row),
                              "iopv": row["iopv"], "iopv_at": row["iopv_at"], "iopv_source": row["iopv_source"],
                              "nav_date_known": bool(row["observed_at"]), "kind": info["kind"], "bucket": info["bucket"]})
            orders = [dict(r) for r in conn.execute("SELECT id,code,side,status,quantity,amount,limit_price,fee,premium_pct,nav,units,reserved,created_at,updated_at,error FROM orders ORDER BY created_at DESC LIMIT 100").fetchall()]
            flows = [dict(r) for r in conn.execute("SELECT * FROM flows ORDER BY created_at DESC LIMIT 100").fetchall()]
            settings = read_settings(conn)
            nav_history = [dict(r) for r in conn.execute("SELECT * FROM nav_points ORDER BY id DESC LIMIT 50").fetchall()]
            return {"account": {k: (float(v) if isinstance(v, Decimal) else v) for k, v in metrics.items() if k != "positions"},
                    "positions": metrics["positions"], "quotes": quotes, "orders": orders, "flows": flows,
                    "settings": settings, "targets": LONG_TERM_TARGETS, "caps": CAPS, "monthly_budget": MONTHLY_BUDGET,
                    "instruments": {k: {**v, "research_approved": v.get("research_approved", False)} for k, v in INSTRUMENTS.items()},
                    "nav_history": nav_history, "notice": "全部订单和成交均为模拟；快照不是盘口委托保证。"}

    @app.post("/api/deposit")
    def deposit(payload: DepositIn) -> dict[str, Any]:
        with db_conn(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            metrics = account_metrics(conn)
            amount = money(payload.amount)
            acct = conn.execute("SELECT * FROM account WHERE id=1").fetchone()
            units = D(acct["units"])
            price = metrics["unit_nav"] if units > 0 else D("1")
            minted = amount / price if price > 0 else amount
            conn.execute("UPDATE account SET cash=?,units=? WHERE id=1", (str(D(acct["cash"]) + amount), str(units + minted)))
            conn.execute("INSERT INTO flows(id,kind,amount,note,created_at) VALUES(?,?,?,?,?)", (str(uuid.uuid4()), "deposit", str(amount), payload.note, now_iso()))
            record_nav(conn)
            return {"ok": True, "amount": str(amount), "unit_price": str(price), "units_issued": str(minted)}

    @app.post("/api/route-preview")
    def route_preview(payload: RouteIn) -> dict[str, Any]:
        with db_conn(db_path) as conn:
            return calculate_route(conn, payload.amount)

    @app.post("/api/settings")
    def update_settings(payload: SettingsIn) -> dict[str, Any]:
        with db_conn(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            settings = read_settings(conn)
            requested = payload.model_dump(exclude_none=True)
            if requested.get("stamp_duty_verified") is True and requested.get("stamp_duty_rate", settings.get("stamp_duty_rate", "")) in (None, ""):
                raise HTTPException(400, "标记卖出税费已核验前，必须填写核验后的印花税率")
            for key, value in requested.items():
                settings[key] = str(value) if isinstance(value, Decimal) else value
            conn.execute("UPDATE settings SET value_json=? WHERE id=1", (json.dumps(settings, ensure_ascii=False),))
            return {"ok": True, "settings": settings}

    @app.post("/api/quotes/manual")
    def manual_quote(payload: QuoteIn) -> dict[str, Any]:
        instrument_info(payload.code)
        observed = parse_time(payload.observed_at)
        age = (datetime.now(timezone.utc) - observed).total_seconds()
        if age < -2 or age > STALE_SECONDS:
            raise HTTPException(400, "录入价格须为当前 60 秒内的报价；旧数据会被明确标为过期")
        with db_conn(db_path) as conn:
            old = quote_row(conn, payload.code)
            conn.execute("INSERT INTO quotes(code,price,observed_at,source,request_id,raw_json,iopv,iopv_at,iopv_source) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET price=excluded.price,observed_at=excluded.observed_at,source=excluded.source,request_id=excluded.request_id,raw_json=excluded.raw_json,iopv=CASE WHEN excluded.code='513800' THEN quotes.iopv ELSE NULL END,iopv_at=CASE WHEN excluded.code='513800' THEN quotes.iopv_at ELSE NULL END,iopv_source=CASE WHEN excluded.code='513800' THEN quotes.iopv_source ELSE NULL END", (
                payload.code, str(payload.price), observed.isoformat(timespec="seconds"), payload.source_note, "manual", json.dumps({"manual": True}),
                old["iopv"] if old and payload.code == "513800" else None, old["iopv_at"] if old and payload.code == "513800" else None, old["iopv_source"] if old and payload.code == "513800" else None))
            return {"ok": True, "code": payload.code, "price": str(payload.price), "observed_at": observed.isoformat(timespec="seconds"), "source": payload.source_note}

    @app.post("/api/iopv")
    def record_iopv(payload: IopvIn) -> dict[str, Any]:
        observed = parse_time(payload.observed_at)
        age = (datetime.now(timezone.utc) - observed).total_seconds()
        if age < -2 or age > STALE_SECONDS:
            raise HTTPException(400, "IOPV 必须来自当前 60 秒内的同一时点")
        with db_conn(db_path) as conn:
            quote = quote_row(conn, payload.code)
            if not quote or not is_fresh(quote):
                raise HTTPException(400, "请先录入/刷新同一时点的场内价格")
            if abs((parse_time(quote["observed_at"]) - observed).total_seconds()) > STALE_SECONDS:
                raise HTTPException(400, "场内价与 IOPV 的时间差超过 60 秒")
            conn.execute("UPDATE quotes SET iopv=?,iopv_at=?,iopv_source=? WHERE code=?", (str(payload.iopv), observed.isoformat(timespec="seconds"), payload.source_note, payload.code))
            premium = (D(quote["price"]) / payload.iopv - 1) * 100
            return {"ok": True, "premium_pct": str(premium.quantize(D("0.001"), rounding=ROUND_HALF_UP)), "within_limit": premium <= D("2")}

    @app.post("/api/quotes/refresh")
    def refresh_quotes() -> dict[str, Any]:
        cli = cli_base_command()
        commands = [
            (cli + ["--format", "json", "--source", "remote", "--no-input", "market", "snapshot", "--thscodes", "601398.SH,600900.SH,000333.SZ,600690.SH,600660.SH"], "stocks"),
            (cli + ["--format", "json", "--source", "remote", "--no-input", "fund", "snapshot", "--thscode", "513800.SH"], "513800"),
        ]
        results = []
        for command, label in commands:
            try:
                completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
                envelope = json.loads(completed.stdout) if completed.returncode == 0 else None
                if not envelope or not envelope.get("ok"):
                    results.append({"group": label, "ok": False, "error": (completed.stderr or completed.stdout or "上游返回失败")[-800:]})
                    continue
                stamp = envelope.get("data", {}).get("timestamp")
                observed = datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat(timespec="seconds") if stamp else now_iso()
                with db_conn(db_path) as conn:
                    for item in envelope.get("data", {}).get("item", []):
                        code = item.get("ticker") or item.get("thscode", "").split(".")[0]
                        if code not in INSTRUMENTS or not item.get("last_price"):
                            continue
                        conn.execute("INSERT INTO quotes(code,price,observed_at,source,request_id,raw_json,iopv,iopv_at,iopv_source) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET price=excluded.price,observed_at=excluded.observed_at,source=excluded.source,request_id=excluded.request_id,raw_json=excluded.raw_json,iopv=NULL,iopv_at=NULL,iopv_source=NULL", (
                            code, str(item["last_price"]), observed, "hithink-finance remote snapshot", envelope.get("meta", {}).get("request_id"), json.dumps(item, ensure_ascii=False), None, None, None))
                results.append({"group": label, "ok": True, "observed_at": observed, "request_id": envelope.get("meta", {}).get("request_id"), "count": len(envelope.get("data", {}).get("item", []))})
            except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                results.append({"group": label, "ok": False, "error": str(exc)})
        return {"ok": all(r["ok"] for r in results), "results": results, "note": "刷新不会提供 513800 IOPV；需从券商同一时点录入。失败时保留上次快照并显示过期。"}

    @app.post("/api/orders/preview")
    def order_preview(payload: PreviewIn) -> dict[str, Any]:
        with db_conn(db_path) as conn:
            return preview_order(conn, payload)

    @app.post("/api/orders")
    def submit_order(payload: PreviewIn) -> dict[str, Any]:
        with db_conn(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if payload.idempotency_key:
                existing = conn.execute("SELECT id,status FROM orders WHERE id=?", (payload.idempotency_key,)).fetchone()
                if existing:
                    return {"ok": True, "id": existing["id"], "status": existing["status"], "duplicate": True}
            result = preview_order(conn, payload)
            if not result["ok"]:
                raise HTTPException(400, {"message": "预检未通过", "reasons": result["reasons"]})
            order_id = payload.idempotency_key or str(uuid.uuid4())
            info = instrument_info(payload.code)
            account = conn.execute("SELECT cash FROM account WHERE id=1").fetchone()
            cash = D(account["cash"])
            reserve = D(result["reserved"])
            if payload.side == "buy":
                conn.execute("UPDATE account SET cash=? WHERE id=1", (str(cash - reserve),))
            conn.execute("INSERT INTO orders(id,code,side,status,quantity,amount,limit_price,fee,fee_rate,premium_pct,nav,units,reserved,created_at,updated_at,source_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                order_id, payload.code, payload.side, result["status_after_submit"], result["quantity"], result["amount"], result["limit_price"], result["fee"], result["fee_rate"], result["premium_pct"], None, None,
                str(reserve), now_iso(), now_iso(), json.dumps({"preview": result, "idempotency_key": payload.idempotency_key}, ensure_ascii=False)))
            record_nav(conn)
            return {"ok": True, "id": order_id, "status": result["status_after_submit"], "preview": result}

    @app.post("/api/orders/{order_id}/fill")
    def fill_order(order_id: str) -> dict[str, Any]:
        with db_conn(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order:
                raise HTTPException(404, "找不到模拟订单")
            if order["status"] != "pending_fill":
                raise HTTPException(400, "该订单当前状态不可模拟成交")
            info = instrument_info(order["code"])
            quote = quote_row(conn, order["code"])
            if not is_fresh(quote):
                raise HTTPException(400, "价格已过期；刷新后再模拟成交")
            ref = D(quote["price"])
            limit = D(order["limit_price"])
            if order["side"] == "buy" and limit < ref:
                raise HTTPException(400, "买入限价低于当前参考价，保持未成交")
            if order["side"] == "sell" and limit > ref:
                raise HTTPException(400, "卖出限价高于当前参考价，保持未成交")
            if info["kind"] == "etf" and order["side"] == "buy":
                if not quote["iopv"] or not quote["iopv_at"] or abs((parse_time(quote["observed_at"]) - parse_time(quote["iopv_at"])).total_seconds()) > STALE_SECONDS:
                    raise HTTPException(400, "成交前必须重新确认同一时点 IOPV")
                premium = (ref / D(quote["iopv"]) - 1) * 100
                if premium > D("2"):
                    raise HTTPException(400, "成交时溢价已超过 2%，拒绝模拟成交")
            qty = D(order["quantity"])
            notional = qty * ref
            settings = read_settings(conn)
            fee = max(notional * D(settings["commission_rate"]), D(settings["minimum_commission"])) if info["kind"] != "fund" else D(order["fee"])
            if order["side"] == "buy":
                reserve = D(order["reserved"])
                actual_cost = notional + fee
                if actual_cost > reserve:
                    raise HTTPException(400, "按当前参考价计算的金额超过冻结金额，请撤单并重新下单")
                conn.execute("UPDATE account SET cash=cash+? WHERE id=1", (str(reserve - actual_cost),))
                old = conn.execute("SELECT * FROM holdings WHERE code=?", (order["code"],)).fetchone()
                old_qty = D(old["quantity"]) if old else D("0")
                old_cost = D(old["avg_cost"]) if old else D("0")
                new_qty = old_qty + qty
                avg_cost = (old_qty * old_cost + actual_cost) / new_qty if new_qty else D("0")
                conn.execute("INSERT INTO holdings(code,quantity,avg_cost) VALUES(?,?,?) ON CONFLICT(code) DO UPDATE SET quantity=excluded.quantity,avg_cost=excluded.avg_cost", (order["code"], str(new_qty), str(avg_cost)))
            else:
                old = conn.execute("SELECT * FROM holdings WHERE code=?", (order["code"],)).fetchone()
                if not old or D(old["quantity"]) < qty:
                    raise HTTPException(400, "持仓不足，不能模拟卖出")
                tax = D("0")
                if info["kind"] == "stock":
                    if not settings.get("stamp_duty_verified"):
                        raise HTTPException(400, "卖出前需核验并录入当日印花税")
                    tax = notional * D(settings["stamp_duty_rate"])
                remain = D(old["quantity"]) - qty
                if remain == 0:
                    conn.execute("DELETE FROM holdings WHERE code=?", (order["code"],))
                else:
                    conn.execute("UPDATE holdings SET quantity=? WHERE code=?", (str(remain), order["code"]))
                conn.execute("UPDATE account SET cash=cash+? WHERE id=1", (str(notional - fee - tax),))
            conn.execute("UPDATE orders SET status='filled',fee=?,reserved='0',updated_at=? WHERE id=?", (str(money(fee)), now_iso(), order_id))
            conn.execute("INSERT INTO flows(id,kind,amount,note,created_at) VALUES(?,?,?,?,?)", (str(uuid.uuid4()), "trade", str(-fee if order["side"] == "buy" else notional - fee), f"{order['side']} {order['code']} 模拟成交", now_iso()))
            record_nav(conn)
            return {"ok": True, "id": order_id, "status": "filled", "reference_fill_price": str(ref), "quantity": str(qty), "fee": str(money(fee)), "note": "假设按当前参考价成交，不代表交易所撮合结果"}

    @app.post("/api/orders/{order_id}/confirm")
    def confirm_fund(order_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        nav = as_decimal(payload.get("nav"), "确认净值")
        nav_date = str(payload.get("nav_date", ""))
        evidence = str(payload.get("evidence", ""))
        if nav <= 0 or not nav_date or not evidence:
            raise HTTPException(400, "需填写正数确认净值、净值日期和确认依据")
        with db_conn(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order or order["status"] != "pending_confirmation" or instrument_info(order["code"])["kind"] != "fund":
                raise HTTPException(400, "该订单不是待确认的基金申购")
            gross = D(order["amount"])
            rate = D(order["fee_rate"])
            net = gross / (1 + rate)
            fee = gross - net
            units = net / nav
            old = conn.execute("SELECT * FROM holdings WHERE code=?", (order["code"],)).fetchone()
            old_qty = D(old["quantity"]) if old else D("0")
            old_cost = D(old["avg_cost"]) if old else D("0")
            total_units = old_qty + units
            cost_per_unit = (old_qty * old_cost + (gross - fee)) / total_units
            conn.execute("INSERT INTO holdings(code,quantity,avg_cost) VALUES(?,?,?) ON CONFLICT(code) DO UPDATE SET quantity=excluded.quantity,avg_cost=excluded.avg_cost", (order["code"], str(total_units), str(cost_per_unit)))
            conn.execute("UPDATE orders SET status='filled',fee=?,nav=?,units=?,reserved='0',updated_at=?,source_json=json_set(source_json,'$.confirmation',?) WHERE id=?", (str(money(fee)), str(nav), str(units), now_iso(), json.dumps({"nav_date": nav_date, "evidence": evidence}, ensure_ascii=False), order_id))
            conn.execute("INSERT INTO flows(id,kind,amount,note,created_at) VALUES(?,?,?,?,?)", (str(uuid.uuid4()), "fund_confirmation", str(-fee), f"{order['code']} 确认 {units:.6f} 份；依据：{evidence}", now_iso()))
            record_nav(conn)
            return {"ok": True, "id": order_id, "status": "filled", "nav": str(nav), "nav_date": nav_date, "units": str(units), "fee": str(money(fee))}

    @app.post("/api/orders/{order_id}/cancel")
    def cancel_order(order_id: str) -> dict[str, Any]:
        with db_conn(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order or order["status"] not in ("pending_fill", "pending_confirmation"):
                raise HTTPException(400, "只有未完成订单可以撤销")
            if order["side"] == "buy":
                account = conn.execute("SELECT cash FROM account WHERE id=1").fetchone()
                conn.execute("UPDATE account SET cash=? WHERE id=1", (str(D(account["cash"]) + D(order["reserved"])),))
            conn.execute("UPDATE orders SET status='cancelled',reserved='0',updated_at=? WHERE id=?", (now_iso(), order_id))
            conn.execute("INSERT INTO flows(id,kind,amount,note,created_at) VALUES(?,?,?,?,?)", (str(uuid.uuid4()), "cancel", "0", f"撤销订单 {order_id}", now_iso()))
            record_nav(conn)
            return {"ok": True, "id": order_id, "status": "cancelled", "released": order["reserved"]}

    return app


app = create_app()
