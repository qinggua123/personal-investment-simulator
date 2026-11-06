"""Policy-event evidence, point-in-time exposure, and exploratory event studies."""
from __future__ import annotations

import csv
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics
import threading
import uuid
from typing import Any
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
EVENTS_PATH = DATA_DIR / "policy_events.csv"
UNIVERSE_PATH = DATA_DIR / "policy_universe.csv"
CN_TZ = ZoneInfo("Asia/Shanghai")
DB_LOCK = threading.RLock()
METHOD_VERSION = "market-model-event-study-v1.0"
WINDOWS = ((-20, -2), (-1, 1), (0, 5), (0, 20), (0, 60))
MIN_ESTIMATION_OBSERVATIONS = 120
POLICY_HALF_LIFE = 20
POLICY_ACTIVE_SESSIONS = 60


def _now() -> str:
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def _connect(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    return closing(sqlite3_connect(path))


def sqlite3_connect(path: Path):
    import sqlite3
    return sqlite3.connect(path, timeout=30)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def _flag(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def _public_time(value: str, date_only: bool = False) -> datetime:
    raw = str(value).strip()
    if not raw:
        raise ValueError("时间不能为空")
    is_date = date_only or len(raw) == 10
    if is_date:
        parsed = date.fromisoformat(raw[:10])
        dt = datetime.combine(parsed, time(23, 59), tzinfo=CN_TZ)
    else:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        dt = dt.replace(tzinfo=CN_TZ) if dt.tzinfo is None else dt.astimezone(CN_TZ)
    return dt


def load_events() -> list[dict[str, Any]]:
    with EVENTS_PATH.open("r", encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    result = []
    seen = set()
    for row in rows:
        event_id = str(row.get("event_id", "")).strip()
        if not event_id or event_id in seen:
            raise ValueError(f"政策种子包含空事件 ID 或重复 ID：{event_id}")
        seen.add(event_id)
        _public_time(row.get("published_at", ""), _flag(row.get("date_only")))
        if row.get("direction") not in {"support", "restrict", "mixed", "neutral"}:
            raise ValueError(f"政策事件方向无效：{event_id}")
        result.append({**row, "reviewed": _flag(row.get("reviewed")),
                       "date_only": _flag(row.get("date_only"))})
    return result


def load_universe() -> list[dict[str, str]]:
    with UNIVERSE_PATH.open("r", encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    seen = set()
    result = []
    for row in rows:
        code = str(row.get("code", "")).strip().zfill(6)
        if len(code) != 6 or not code.isdigit() or code in seen:
            raise ValueError(f"政策研究股票池代码无效或重复：{code}")
        seen.add(code)
        result.append({"code": code, "name": row.get("name", ""),
                       "sector": row.get("sector", ""), "role": row.get("role", "")})
    return result


def _event_map() -> dict[str, dict[str, Any]]:
    return {row["event_id"]: row for row in load_events()}


def _company_map() -> dict[str, dict[str, str]]:
    return {row["code"]: row for row in load_universe()}


def _thscode(code: str) -> str:
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    exchange = "SH" if code.startswith(("5", "6", "9")) else "SZ"
    return f"{code}.{exchange}"


def _normalize_code(value: Any) -> str:
    raw = str(value).strip().upper()
    if "." in raw:
        raw = raw.split(".", 1)[0]
    return raw.zfill(6)


def initialize(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with DB_LOCK, _connect(path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS policy_links (
            event_id TEXT NOT NULL, code TEXT NOT NULL, strength REAL NOT NULL,
            channel TEXT NOT NULL, business_evidence TEXT NOT NULL, evidence_url TEXT NOT NULL DEFAULT '',
            evidence_available_at TEXT NOT NULL, reviewed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(event_id, code))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS policy_studies (
            id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            status TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '',
            request_json TEXT NOT NULL, input_json TEXT NOT NULL,
            result_json TEXT, error TEXT)""")
        conn.commit()


def _links(path: Path) -> list[dict[str, Any]]:
    initialize(path)
    import sqlite3
    with _connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM policy_links ORDER BY event_id, code").fetchall()
    return [{**dict(row), "reviewed": bool(row["reviewed"])} for row in rows]


def get_overview(path: Path) -> dict[str, Any]:
    events, companies, links = load_events(), load_universe(), _links(path)
    by_event: dict[str, list[dict[str, Any]]] = {}
    for link in links:
        by_event.setdefault(link["event_id"], []).append(link)
    for event in events:
        rows = by_event.get(event["event_id"], [])
        event["link_count"] = len(rows)
        event["reviewed_link_count"] = sum(1 for row in rows if row["reviewed"])
    studies = list_studies(path)
    return {"events": events, "companies": companies, "links": links, "studies": studies,
            "summary": {"event_count": len(events), "company_count": len(companies),
                        "link_count": len(links),
                        "reviewed_link_count": sum(1 for link in links if link["reviewed"]),
                        "source_documents_archived": 0,
                        "latest_event": max((e["published_at"] for e in events), default=None)}}


def validate_link(payload: dict[str, Any]) -> dict[str, Any]:
    events, companies = _event_map(), _company_map()
    event_id = str(payload.get("event_id", "")).strip()
    code = str(payload.get("code", "")).strip().zfill(6)
    if event_id not in events:
        raise ValueError("找不到该政策事件")
    if code not in companies:
        raise ValueError("公司不在政策研究股票池中")
    try:
        strength = float(payload.get("strength"))
    except (TypeError, ValueError) as exc:
        raise ValueError("关联强度必须是 0.5 或 1") from exc
    if strength not in {0.5, 1.0}:
        raise ValueError("关联强度必须是 0.5 或 1")
    channel = str(payload.get("channel", "")).strip()
    evidence = str(payload.get("business_evidence", "")).strip()
    evidence_at = str(payload.get("evidence_available_at", "")).strip()
    if not channel or not evidence or len(evidence) < 8:
        raise ValueError("请填写关联渠道和至少 8 个字的业务证据")
    evidence_time = _public_time(evidence_at)
    event = events[event_id]
    published = _public_time(event["published_at"], event["date_only"])
    reviewed = bool(payload.get("reviewed"))
    if reviewed and evidence_time > published:
        raise ValueError("复核通过的历史关联证据日期不能晚于政策首次公开时间")
    evidence_url = str(payload.get("evidence_url", "")).strip()
    if evidence_url and not evidence_url.lower().startswith(("https://", "http://")):
        raise ValueError("证据链接必须以 http:// 或 https:// 开头")
    if reviewed and not evidence_url:
        raise ValueError("复核通过的公司关联必须提供可追溯的证据链接")
    return {"event_id": event_id, "code": code, "strength": strength,
            "channel": channel, "business_evidence": evidence, "evidence_url": evidence_url,
            "evidence_available_at": evidence_at, "reviewed": reviewed}


def upsert_link(payload: dict[str, Any], path: Path) -> dict[str, Any]:
    item = validate_link(payload)
    now = _now()
    with DB_LOCK, _connect(path) as conn:
        conn.execute("""INSERT INTO policy_links
            (event_id,code,strength,channel,business_evidence,evidence_url,evidence_available_at,reviewed,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(event_id,code) DO UPDATE SET strength=excluded.strength,
                channel=excluded.channel,business_evidence=excluded.business_evidence,
                evidence_url=excluded.evidence_url,evidence_available_at=excluded.evidence_available_at,
                reviewed=excluded.reviewed,updated_at=excluded.updated_at""",
            (item["event_id"], item["code"], item["strength"], item["channel"],
             item["business_evidence"], item["evidence_url"], item["evidence_available_at"],
             int(item["reviewed"]), now, now))
        conn.commit()
    return next(link for link in _links(path)
                if link["event_id"] == item["event_id"] and link["code"] == item["code"])


def delete_link(event_id: str, code: str, path: Path) -> bool:
    with DB_LOCK, _connect(path) as conn:
        cursor = conn.execute("DELETE FROM policy_links WHERE event_id=? AND code=?",
                              (event_id, str(code).zfill(6)))
        conn.commit()
    return cursor.rowcount > 0


def _select_inputs(request: dict[str, Any], path: Path) -> dict[str, Any]:
    events, links = load_events(), _links(path)
    event_ids = set(request.get("event_ids") or [])
    codes = {_normalize_code(code) for code in (request.get("codes") or [])}
    if event_ids:
        events = [event for event in events if event["event_id"] in event_ids]
    if codes:
        links = [link for link in links if link["code"] in codes]
    selected_ids = {event["event_id"] for event in events}
    links = [link for link in links if link["event_id"] in selected_ids]
    source_bytes = EVENTS_PATH.read_bytes() + UNIVERSE_PATH.read_bytes()
    return {"events": events, "links": links, "universe": load_universe(),
            "seed_hash": hashlib.sha256(source_bytes).hexdigest(),
            "method_version": METHOD_VERSION}


def create_study(request: dict[str, Any], path: Path) -> str:
    initialize(path)
    study_id, now = uuid.uuid4().hex, _now()
    request_row = {**request, "evaluation_date": str(request.get("evaluation_date") or date.today())}
    snapshot = _select_inputs(request, path)
    snapshot["input_hash"] = _canonical_hash({"request": request_row, "policy_inputs": snapshot})
    with DB_LOCK, _connect(path) as conn:
        conn.execute("""INSERT INTO policy_studies
            (id,created_at,updated_at,status,progress,message,request_json,input_json)
            VALUES(?,?,?,?,?,?,?,?)""",
            (study_id, now, now, "queued", 0, "排队中", _dump(request_row), _dump(snapshot)))
        conn.commit()
    return study_id


def get_study(study_id: str, path: Path) -> dict[str, Any]:
    initialize(path)
    import sqlite3
    with _connect(path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM policy_studies WHERE id=?", (study_id,)).fetchone()
    if row is None:
        raise KeyError(study_id)
    item = dict(row)
    item["request"] = json.loads(item.pop("request_json"))
    item["inputs"] = json.loads(item.pop("input_json"))
    item["result"] = json.loads(item.pop("result_json")) if item.get("result_json") else None
    item.pop("result_json", None)
    return item


def list_studies(path: Path, limit: int = 20) -> list[dict[str, Any]]:
    initialize(path)
    import sqlite3
    with _connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""SELECT id,created_at,updated_at,status,progress,message,error
            FROM policy_studies ORDER BY created_at DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(row) for row in rows]


def _set_study(study_id: str, path: Path, *, status: str | None = None,
               progress: float | None = None, message: str | None = None,
               result: dict[str, Any] | None = None, error: str | None = None) -> None:
    fields, values = ["updated_at=?"], [_now()]
    for key, value in (("status", status), ("progress", progress), ("message", message),
                       ("result_json", _dump(result) if result is not None else None),
                       ("error", error[:2000] if error is not None else None)):
        if value is not None:
            fields.append(f"{key}=?")
            values.append(value)
    values.append(study_id)
    with DB_LOCK, _connect(path) as conn:
        conn.execute(f"UPDATE policy_studies SET {','.join(fields)} WHERE id=?", values)
        conn.commit()


def _reaction_index(published_at: str, date_only: bool, sessions: list[str]) -> int | None:
    published = _public_time(published_at, date_only)
    event_day = published.date()
    next_session = date_only or published.hour >= 15
    for index, session in enumerate(sessions):
        session_day = date.fromisoformat(session)
        if session_day > event_day or (session_day == event_day and not next_session):
            return index
    return None


def _returns(sessions: list[str], bars: list[dict[str, Any]]) -> list[float | None]:
    close_by_day = {str(row["date"]): float(row["close"]) for row in bars
                    if row.get("date") and row.get("close") is not None and float(row["close"]) > 0}
    result: list[float | None] = [None]
    for previous_day, current_day in zip(sessions, sessions[1:]):
        previous, current = close_by_day.get(previous_day), close_by_day.get(current_day)
        result.append(current / previous - 1 if previous and current else None)
    return result


def _student_t_two_sided_p(value: float, degrees: int) -> float:
    if degrees <= 0:
        return 1.0
    if value <= 0:
        return 1.0
    log_norm = (math.lgamma((degrees + 1) / 2) - math.lgamma(degrees / 2)
                - 0.5 * math.log(degrees * math.pi))
    norm = math.exp(log_norm)
    low, high = math.atan(value), math.pi / 2
    steps = 256
    width = (high - low) / steps
    def density(angle: float) -> float:
        cosine = max(0.0, math.cos(angle))
        return norm * (cosine ** (degrees - 1))
    total = density(low) + density(high)
    total += 4 * sum(density(low + i * width) for i in range(1, steps, 2))
    total += 2 * sum(density(low + i * width) for i in range(2, steps, 2))
    return min(1.0, max(0.0, 2 * width * total / 3))


def _critical_t_95(degrees: int) -> float:
    low, high = 0.0, 10.0
    for _ in range(40):
        middle = (low + high) / 2
        if _student_t_two_sided_p(middle, degrees) > 0.05:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def _estimate(event: dict[str, Any], link: dict[str, Any], session_index: int,
              sessions: list[str], stock_returns: list[float | None],
              market_returns: list[float | None], window: tuple[int, int]) -> dict[str, Any]:
    left, right = window
    row = {"event_id": event["event_id"], "family": event["family"], "code": link["code"],
           "window": f"{left}:{right}", "event_session": sessions[session_index],
           "link_strength": link["strength"]}
    if session_index < 31 or session_index + left < 0 or session_index + right >= len(sessions):
        return {**row, "status": "skipped", "reason": "历史估计期或事件窗口超出行情覆盖"}
    estimate_indices = range(max(0, session_index - 250), session_index - 30)
    estimate = [(stock_returns[i], market_returns[i]) for i in estimate_indices
                if stock_returns[i] is not None and market_returns[i] is not None]
    if len(estimate) < MIN_ESTIMATION_OBSERVATIONS:
        return {**row, "status": "skipped", "reason": f"市场模型估计观测少于 {MIN_ESTIMATION_OBSERVATIONS} 个"}
    event_indices = range(session_index + left, session_index + right + 1)
    observed = [(stock_returns[i], market_returns[i]) for i in event_indices]
    if any(stock is None or market is None for stock, market in observed):
        return {**row, "status": "skipped", "reason": "事件窗口行情缺失"}
    n = len(estimate)
    mean_market = sum(market for _, market in estimate) / n
    mean_stock = sum(stock for stock, _ in estimate) / n
    sxx = sum((market - mean_market) ** 2 for _, market in estimate)
    if sxx <= 1e-20:
        return {**row, "status": "skipped", "reason": "市场模型估计期基准收益无变异"}
    beta = sum((market - mean_market) * (stock - mean_stock)
               for stock, market in estimate) / sxx
    alpha = mean_stock - beta * mean_market
    residuals = [stock - alpha - beta * market for stock, market in estimate]
    residual_variance = sum(value * value for value in residuals) / (n - 2)
    abnormal = [stock - alpha - beta * market for stock, market in observed]
    car = sum(abnormal)
    k = len(observed)
    event_market_sum = sum(market for _, market in observed)
    prediction_term = k + (k * k / n) + ((event_market_sum - k * mean_market) ** 2 / sxx)
    standard_error = math.sqrt(max(0.0, residual_variance * prediction_term))
    statistic = abs(car / standard_error) if standard_error > 0 else 0.0
    p_value = _student_t_two_sided_p(statistic, n - 2) if standard_error > 0 else float(car == 0)
    critical = _critical_t_95(n - 2)
    actual_return = math.prod(1 + stock for stock, _ in observed) - 1
    return {**row, "status": "exploratory", "reason": "", "n_estimation": n,
            "abnormal_returns": abnormal, "car": car, "actual_return": actual_return,
            "alpha": alpha, "beta": beta, "ci_low": car - critical * standard_error,
            "ci_high": car + critical * standard_error, "p_value": p_value,
            "q_bh": None}


def _bh_adjust(results: list[dict[str, Any]]) -> None:
    valid = sorted((row for row in results if row.get("status") == "exploratory"),
                   key=lambda row: float(row["p_value"]))
    count = len(valid)
    running = 1.0
    for rank in range(count, 0, -1):
        row = valid[rank - 1]
        running = min(running, float(row["p_value"]) * count / rank)
        row["q_bh"] = min(1.0, running)


def _eligible(event: dict[str, Any], link: dict[str, Any]) -> tuple[bool, str]:
    if not event["reviewed"]:
        return False, "政策事件未复核"
    if not link["reviewed"]:
        return False, "公司关联未复核"
    if not str(link.get("business_evidence", "")).strip():
        return False, "缺少公司业务证据"
    if not str(link.get("evidence_url", "")).strip():
        return False, "缺少可追溯的公司证据链接"
    if float(link["strength"]) <= 0:
        return False, "关联强度为零"
    try:
        evidence_time = _public_time(link["evidence_available_at"])
        published = _public_time(event["published_at"], event["date_only"])
    except (ValueError, TypeError):
        return False, "事件或证据时间无效"
    if evidence_time > published:
        return False, "关联证据晚于政策首次公开时间"
    return True, ""


def _policy_exposures(events: list[dict[str, Any]], links: list[dict[str, Any]],
                      companies: list[dict[str, str]], sessions: list[str],
                      evaluation_date: str) -> list[dict[str, Any]]:
    if not sessions:
        return [{**company, "status": "unavailable", "score": None,
                 "event_ids": [], "reason": "缺少沪深300交易日历"} for company in companies]
    by_id = {event["event_id"]: event for event in events}
    by_code: dict[str, list[dict[str, Any]]] = {}
    for link in links:
        by_code.setdefault(link["code"], []).append(link)
    last_day = min(evaluation_date, sessions[-1])
    last_index = max((i for i, session in enumerate(sessions) if session <= last_day), default=-1)
    outputs = []
    for company in companies:
        code_links = by_code.get(company["code"], [])
        known = []
        for link in code_links:
            event = by_id.get(link["event_id"])
            if (event and _public_time(event["published_at"], event["date_only"]).date().isoformat() <= evaluation_date
                    and _eligible(event, link)[0]):
                known.append((event, link))
        if not known:
            outputs.append({**company, "status": "missing_evidence", "score": None,
                            "event_ids": [], "reason": "没有已知且复核通过的公司关联证据"})
            continue
        score, ids = 0.0, []
        for event, link in known:
            start = _reaction_index(event["published_at"], event["date_only"], sessions)
            if start is None or last_index < start:
                continue
            age = last_index - start
            if age >= POLICY_ACTIVE_SESSIONS:
                continue
            direction = {"support": 1.0, "restrict": -1.0}.get(event["direction"], 0.0)
            score += direction * float(link["strength"]) * 2 ** (-age / POLICY_HALF_LIFE)
            ids.append(event["event_id"])
        outputs.append({**company, "status": "available", "score": score,
                        "event_ids": ids, "reason": ""})
    return outputs


def execute_study(study_id: str, path: Path) -> None:
    try:
        import sqlite3
        row = get_study(study_id, path)
        request, inputs = row["request"], row["inputs"]
        _set_study(study_id, path, status="running", progress=5, message="检查政策关联证据")
        events = inputs["events"]
        links = inputs["links"]
        event_by_id = {event["event_id"]: event for event in events}
        selected_codes = {_normalize_code(code) for code in (request.get("codes") or [])}
        selected_events = set(request.get("event_ids") or [])
        evaluation_date = str(request.get("evaluation_date") or date.today())
        end = date.fromisoformat(evaluation_date)
        if end > date.today():
            raise ValueError("研究截止日不能晚于今天")
        exclusions = []
        eligible = []
        for link in links:
            if selected_events and link["event_id"] not in selected_events:
                continue
            if selected_codes and _normalize_code(link["code"]) not in selected_codes:
                continue
            event = event_by_id.get(link["event_id"])
            if not event:
                exclusions.append({"event_id": link["event_id"], "code": link["code"], "reason": "找不到政策事件"})
                continue
            if _public_time(event["published_at"], event["date_only"]).date() > end:
                exclusions.append({"event_id": event["event_id"], "code": link["code"],
                                   "reason": "政策事件晚于研究截止日"})
                continue
            ok, reason = _eligible(event, link)
            if ok:
                eligible.append((event, link))
            else:
                exclusions.append({"event_id": event["event_id"], "code": link["code"], "reason": reason})
        if not eligible:
            result = {"status": "blocked_missing_links", "method_version": METHOD_VERSION,
                      "input_hash": inputs["input_hash"], "seed_hash": inputs["seed_hash"],
                      "evaluation_date": evaluation_date, "eligible_link_count": 0,
                      "reviewed_link_count": sum(1 for link in links if link["reviewed"]),
                      "study_results": [], "family_summary": [], "exclusions": exclusions,
                      "exposures": [], "coverage": [], "predictive_layer": {
                          "status": "blocked", "reason": "没有符合时间条件的复核公司关联，不能估计政策效果或策略收益。"},
                      "limitations": _limitations()}
            _set_study(study_id, path, status="blocked_missing_links", progress=100,
                       message="没有可用于研究的复核关联", result=result)
            return

        try:
            from . import stock_research as sr
        except ImportError:
            import stock_research as sr
        event_dates = [_public_time(event["published_at"], event["date_only"]).date()
                       for event, _ in eligible]
        start = min(event_dates) - timedelta(days=500)
        codes = sorted({link["code"] for _, link in eligible})
        _set_study(study_id, path, progress=10, message=f"读取 {len(codes)} 家公司的历史行情和沪深300基准")
        coverage: list[dict[str, Any]] = []
        bars_by_code: dict[str, list[dict[str, Any]]] = {}
        for index, code in enumerate(codes, start=1):
            company = next(item for item in inputs["universe"] if item["code"] == code)
            try:
                payload = sr.load_or_fetch_daily_bars(_thscode(code), start, end,
                                                       refresh=bool(request.get("refresh_data")))
                bars_by_code[code] = payload["prices"]
                coverage.append({"code": code, "name": company["name"],
                                 "first_date": payload["prices"][0]["date"] if payload["prices"] else None,
                                 "last_date": payload["prices"][-1]["date"] if payload["prices"] else None,
                                 "rows": len(payload["prices"]), "request_ids": payload["request_ids"],
                                 "status": "available" if payload["prices"] else "missing_prices"})
            except Exception as exc:
                coverage.append({"code": code, "name": company["name"], "first_date": None,
                                 "last_date": None, "rows": 0, "request_ids": [],
                                 "status": "error", "error": str(exc)[:300]})
            _set_study(study_id, path, progress=10 + 55 * index / len(codes),
                       message=f"读取行情 {index}/{len(codes)}")

        try:
            benchmark_payload = sr.load_or_fetch_daily_bars("000300.SH", start, end,
                instrument_type="index", refresh=bool(request.get("refresh_data")))
            benchmark_bars = benchmark_payload["prices"]
        except Exception as exc:
            benchmark_payload = {"prices": [], "request_ids": [], "error": str(exc)[:300]}
            benchmark_bars = []
        sessions = sorted(str(row["date"]) for row in benchmark_bars if row.get("date"))
        if not sessions:
            result = {"status": "blocked_missing_prices", "method_version": METHOD_VERSION,
                      "input_hash": inputs["input_hash"], "seed_hash": inputs["seed_hash"],
                      "evaluation_date": evaluation_date, "eligible_link_count": len(eligible),
                      "study_results": [], "family_summary": [], "exclusions": exclusions,
                      "exposures": [], "coverage": coverage,
                      "benchmark_error": benchmark_payload.get("error", "缺少沪深300历史行情"),
                      "predictive_layer": {"status": "blocked", "reason": "缺少基准行情。"},
                      "limitations": _limitations()}
            _set_study(study_id, path, status="blocked_missing_prices", progress=100,
                       message="缺少沪深300基准行情", result=result)
            return

        market_returns = _returns(sessions, benchmark_bars)
        stock_returns = {code: _returns(sessions, bars_by_code.get(code, [])) for code in codes}
        study_results = []
        for event, link in eligible:
            event_index = _reaction_index(event["published_at"], event["date_only"], sessions)
            if event_index is None:
                exclusions.append({"event_id": event["event_id"], "code": link["code"],
                                   "reason": "事件日期之后没有沪深300交易日"})
                continue
            if not stock_returns.get(link["code"]):
                exclusions.append({"event_id": event["event_id"], "code": link["code"],
                                   "reason": "缺少公司历史行情"})
                continue
            for window in WINDOWS:
                study_results.append(_estimate(event, link, event_index, sessions,
                    stock_returns[link["code"]], market_returns, window))
        _bh_adjust(study_results)
        family_summary = _family_summary(study_results)
        exposures = _policy_exposures(events, links, inputs["universe"], sessions, evaluation_date)
        success_count = sum(1 for item in study_results if item["status"] == "exploratory")
        status = "done" if success_count else "blocked_missing_prices"
        result = {"status": status, "method_version": METHOD_VERSION,
                  "input_hash": inputs["input_hash"], "seed_hash": inputs["seed_hash"],
                  "evaluation_date": evaluation_date, "eligible_link_count": len(eligible),
                  "reviewed_link_count": sum(1 for link in links if link["reviewed"]),
                  "successful_study_count": success_count, "study_results": study_results,
                  "family_summary": family_summary, "exclusions": exclusions,
                  "exposures": exposures, "coverage": coverage,
                  "benchmark": {"thscode": "000300.SH", "basis": "CSI 300 price index; dividends excluded",
                                "first_date": sessions[0], "last_date": sessions[-1],
                                "rows": len(sessions), "request_ids": benchmark_payload.get("request_ids", [])},
                  "predictive_layer": {"status": "blocked",
                      "reason": "当前结果仅是事件窗口研究。政策预测和组合回测需要独立样本外验证，尚未启用。"},
                  "limitations": _limitations()}
        _set_study(study_id, path, status=status, progress=100,
                   message=f"事件研究完成，有效窗口 {success_count} 个", result=result)
    except Exception as exc:
        _set_study(study_id, path, status="failed", progress=100,
                   message="政策研究失败", error=str(exc))


def _family_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[float]] = {}
    for row in rows:
        if row.get("status") == "exploratory":
            grouped.setdefault((row["family"], row["window"]), []).append(float(row["car"]))
    return [{"family": family, "window": window, "n_company_events": len(values),
             "mean_car": statistics.mean(values), "median_car": statistics.median(values)}
            for (family, window), values in sorted(grouped.items())]


def _limitations() -> list[str]:
    return [
        "政策和公司关系需要逐条人工核验证据；公开资料回溯不等于当时投资者已知。",
        "事件窗口与市场模型只描述历史相关性，不证明政策造成股价变化。",
        "行情使用未复权收盘价计算价格收益，未计分红；拆股、送转和除权除息可能扭曲个别事件窗口，结果不是账户总回报。",
        "同一政策可能与其他公告、市场行情和行业变化重叠，事件样本少时统计结论不稳。",
        "政策预测模型与组合回测没有运行；本模块不会产生买卖信号或订单。",
        "种子只保留官方来源链接，没有复制原项目的本地政策文件缓存。",
    ]
