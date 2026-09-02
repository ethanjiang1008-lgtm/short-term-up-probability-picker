"""全市场短线候选扫描器。"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from features import make_features
from market_data_sina import fetch_all_stocks, fetch_klines_parallel, is_main_board

PRICE_MAX = 50.0
FLOAT_MCAP_MIN_BILLION = 2.0
FLOAT_MCAP_MAX_BILLION = 300.0
MAX_VOLUME_RATIO = 5.0
MAX_TURNOVER_RATE_PCT = 10.0
MIN_EXPECTED_RAW_ROWS = 3000
MIN_KLINE_SUCCESS_RATIO = 0.70
FULL_KLINE_COUNT = 120
KLINE_WORKERS = 10
FOCUS_SCORE_THRESHOLD = 68.0


def _num(v: object, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _change_pct(row: dict) -> float:
    return _num(row.get("change_pct", row.get("changepercent", 0.0)))


def _price(row: dict) -> float:
    return _num(row.get("price", row.get("trade", 0.0)))


def _float_mcap_billion(row: dict) -> float:
    return _num(row.get("circ_mcap", row.get("nmc", 0.0))) / 100000.0


def _is_st_name(name: str) -> bool:
    normalized = "".join(str(name or "").strip().upper().split()).replace("*", "")
    return normalized.startswith("ST")


def _derived_volume_ratio(bars: list[dict]) -> float:
    """透明近似量比：最新成交量 / 前5个交易日平均成交量。"""
    volumes = [_num(b.get("volume")) for b in bars]
    if len(volumes) < 6:
        return float("inf")
    avg_prev5 = sum(volumes[-6:-1]) / 5.0
    if avg_prev5 <= 0:
        return float("inf")
    return volumes[-1] / avg_prev5


def _is_rising_5ma(bars: list[dict]) -> bool:
    """兼容旧测试/调用：判断最新 MA5 是否高于前一交易日 MA5。"""
    closes = [_num(b.get("close")) for b in bars]
    if len(closes) < 6:
        return False
    current = sum(closes[-5:]) / 5.0
    previous = sum(closes[-6:-1]) / 5.0
    return current > previous


def _eligible(row: dict) -> bool:
    code = str(row.get("code", ""))
    name = str(row.get("name", ""))
    if _is_st_name(name) or "退" in name:
        return False
    if not is_main_board(code, name):
        return False
    price = _price(row)
    if price <= 0 or price > PRICE_MAX:
        return False
    mcap = _float_mcap_billion(row)
    if mcap < FLOAT_MCAP_MIN_BILLION or mcap > FLOAT_MCAP_MAX_BILLION:
        return False
    turnover = _num(row.get("turnover_rate", row.get("turnoverratio", 0.0)))
    if turnover <= 0 or turnover >= MAX_TURNOVER_RATE_PCT:
        return False
    return True


def _market_stats(rows: list[dict]) -> dict[str, float | int]:
    valid = [r for r in rows if is_main_board(str(r.get("code", "")), str(r.get("name", "")))]
    up = sum(_change_pct(r) > 0 for r in valid)
    down = sum(_change_pct(r) < 0 for r in valid)
    limit_up = sum(_change_pct(r) >= 9.8 for r in valid)
    limit_down = sum(_change_pct(r) <= -9.8 for r in valid)
    total = up + down
    return {
        "breadth": up / total if total else 0.5,
        "limit_up_count": limit_up,
        "limit_down_count": limit_down,
        "ranked_market_rows": len(rows),
    }


def _grade(score: float) -> str:
    if score >= 75:
        return "A"
    if score >= 68:
        return "B"
    return "C"


def _observation_label(score: float, is_limit_up: bool, technical: dict) -> tuple[str, bool, str]:
    """将模型分数与尾盘交易状态拆成全池状态和重点观察状态。"""
    if is_limit_up:
        return "涨停观察", False, "当日已涨停；保留观察，但不列入重点追踪"
    confirmations = [
        bool(technical.get("ma5_rising", False)),
        bool(technical.get("close_above_ma20", False)),
        bool(technical.get("ma_bull_alignment", False)),
    ]
    confirmation_count = sum(confirmations)
    if score >= FOCUS_SCORE_THRESHOLD and confirmation_count >= 2:
        return "重点观察", True, f"Score≥{FOCUS_SCORE_THRESHOLD:.0f} + 3项趋势确认至少2项"
    if score >= 68:
        return "次重点", False, "模型达到B档，但技术确认不足以进入重点观察"
    return "普通候选", False, "保留在全量候选池，不建议优先跟踪"


def scan_with_diagnostics(max_candidates: int | None = None) -> tuple[list[dict], dict]:
    universe_raw = fetch_all_stocks()
    base_universe = [r for r in universe_raw if _eligible(r)]
    stats = _market_stats(universe_raw)

    base_codes = [str(r.get("code")) for r in base_universe if r.get("code")]
    klines, kline_diag = fetch_klines_parallel(base_codes, count=FULL_KLINE_COUNT, workers=KLINE_WORKERS)
    kline_success_ratio = (
        kline_diag["success"] / kline_diag["requested"] if kline_diag["requested"] else 0.0
    )

    volume_universe = []
    for r in base_universe:
        code = str(r.get("code", ""))
        bars = klines.get(code, [])
        volume_ratio = _derived_volume_ratio(bars)
        if volume_ratio >= MAX_VOLUME_RATIO:
            continue
        row = dict(r)
        row["derived_volume_ratio"] = round(volume_ratio, 4)
        volume_universe.append(row)

    diagnostics = {
        "date": str(date.today()),
        "raw_market_rows": len(universe_raw),
        "main_board_rows": sum(
            is_main_board(str(r.get("code", "")), str(r.get("name", "")))
            for r in universe_raw
        ),
        "eligible_price_mcap_turnover_rows": len(base_universe),
        "rising_5ma_rows": None,
        "final_universe_rows": len(volume_universe),
        "full_pool_rows": len(volume_universe),
        "focus_rows": 0,
        "limit_up_rows": 0,
        "st_rows": 0,
        "score_ge_68_rows": 0,
        "score_ge_75_rows": 0,
        "technical_confirm_2plus_rows": 0,
        "fast_kline_requested": 0,
        "fast_kline_success": 0,
        "fast_kline_failed": 0,
        "fast_kline_success_ratio": None,
        "full_kline_requested": kline_diag["requested"],
        "full_kline_success": kline_diag["success"],
        "full_kline_failed": kline_diag["failed"],
        "full_kline_success_ratio": round(kline_success_ratio, 4),
        "filters": {
            "price_max": PRICE_MAX,
            "float_mcap_billion_min": FLOAT_MCAP_MIN_BILLION,
            "float_mcap_billion_max": FLOAT_MCAP_MAX_BILLION,
            "turnover_rate_pct_lt": MAX_TURNOVER_RATE_PCT,
            "volume_ratio_lt": MAX_VOLUME_RATIO,
            "five_day_ma_rising": False,
            "exclude_st": True,
        },
        "technical_features": {
            "ma5": True,
            "ma10": True,
            "ma20": True,
            "ma60": True,
            "close_above_ma5": True,
            "close_above_ma20": True,
            "ma5_rising": True,
            "ma20_rising": True,
            "ma_bull_alignment": "MA5 > MA10 > MA20 > MA60",
            "relative_volume_5d_vs_20d": True,
            "consecutive_up_days": True,
        },
        "volume_ratio_definition": "latest volume / average volume of previous 5 trading days",
        "kline_strategy": "one 120-bar request per price/mcap/turnover eligible stock; no 5MA hard filter",
        "kline_source": "Sina daily K-line, urllib + SSL fallback + retry, reused from the validated legacy data path",
        "candidate_policy": {
            "full_pool": "保存全部通过硬过滤与量比代理过滤且K线可用的股票",
            "focus": f"score>={FOCUS_SCORE_THRESHOLD:.0f} + 3项趋势确认至少2项，且当日非涨停、非ST",
            "limit_up": "保留在全量池，标记为涨停观察，不进入重点观察",
            "score_is_probability": False,
            "focus_note": "重点观察是交易层优先级，不代表真实概率；弱市时避免因过高绝对门槛导致空集",
        },
        "min_expected_raw_rows": MIN_EXPECTED_RAW_ROWS,
        "min_kline_success_ratio": MIN_KLINE_SUCCESS_RATIO,
        "market": stats,
        "status": "OK",
        "blocking_reason": None,
    }

    if len(universe_raw) < MIN_EXPECTED_RAW_ROWS:
        diagnostics["status"] = "DATA_INCOMPLETE"
        diagnostics["blocking_reason"] = f"raw_market_rows={len(universe_raw)} < {MIN_EXPECTED_RAW_ROWS}"
    elif base_codes and kline_success_ratio < MIN_KLINE_SUCCESS_RATIO:
        diagnostics["status"] = "DATA_INCOMPLETE"
        diagnostics["blocking_reason"] = f"kline_success_ratio={kline_success_ratio:.2%} < {MIN_KLINE_SUCCESS_RATIO:.0%}"

    rows = []
    if diagnostics["status"] != "DATA_INCOMPLETE":
        for r in volume_universe:
            code = str(r.get("code", ""))
            bars = klines.get(code)
            if not bars:
                continue
            f = make_features(
                code,
                str(r.get("name", "")),
                bars,
                sector_change_pct=0.0,
                sector_up_ratio=0.5,
                sector_limit_up_count=0,
                market_breadth=float(stats["breadth"]),
                market_limit_up_count=int(stats["limit_up_count"]),
                market_limit_down_count=int(stats["limit_down_count"]),
                event_score=50.0,
            )
            evidence = f.evidence
            technical = {
                "ma5": round(_num(evidence.get("ma5")), 4),
                "ma10": round(_num(evidence.get("ma10")), 4),
                "ma20": round(_num(evidence.get("ma20")), 4),
                "ma60": round(_num(evidence.get("ma60")), 4),
                "close_above_ma5": bool(evidence.get("close_above_ma5", False)),
                "close_above_ma20": bool(evidence.get("close_above_ma20", False)),
                "ma5_rising": bool(evidence.get("ma5_rising", False)),
                "ma20_rising": bool(evidence.get("ma20_rising", False)),
                "ma_bull_alignment": bool(evidence.get("ma_bull_alignment", False)),
                "relative_volume_5d_vs_20d": round(_num(evidence.get("relative_volume_5d_vs_20d")), 4),
                "consecutive_up_days": int(_num(evidence.get("consecutive_up_days"))),
            }
            score = round(f.score, 2)
            is_limit_up = _change_pct(r) >= 9.8
            observation, is_focus, focus_reason = _observation_label(score, is_limit_up, technical)
            confirmation_count = sum([
                technical["ma5_rising"],
                technical["close_above_ma20"],
                technical["ma_bull_alignment"],
            ])
            diagnostics["score_ge_68_rows"] += int(score >= 68)
            diagnostics["score_ge_75_rows"] += int(score >= 75)
            diagnostics["technical_confirm_2plus_rows"] += int(confirmation_count >= 2)
            rows.append({
                "date": str(date.today()),
                "code": f.code,
                "name": f.name,
                "today_close": _price(r),
                "today_change_pct": round(_change_pct(r), 4),
                "turnover_rate_pct": round(_num(r.get("turnover_rate", 0.0)), 4),
                "volume_ratio": r.get("derived_volume_ratio"),
                "score": score,
                "grade": _grade(score),
                "decision": "FOCUS" if is_focus else ("LIMIT_UP_OBSERVE" if is_limit_up else ("WATCH" if score >= 68 else "NO_TRADE")),
                "observation": observation,
                "is_focus": is_focus,
                "is_limit_up": is_limit_up,
                "focus_reason": focus_reason,
                "probability": None,
                "market": stats,
                "universe": {
                    "raw_rows": len(universe_raw),
                    "eligible_rows_before_volume_filter": len(base_universe),
                    "eligible_rows": len(volume_universe),
                    "price_max": PRICE_MAX,
                    "float_mcap_billion_min": FLOAT_MCAP_MIN_BILLION,
                    "float_mcap_billion_max": FLOAT_MCAP_MAX_BILLION,
                    "turnover_rate_pct_lt": MAX_TURNOVER_RATE_PCT,
                    "volume_ratio_lt": MAX_VOLUME_RATIO,
                    "five_day_ma_rising": False,
                    "exclude_st": True,
                },
                "data_quality": diagnostics,
                "components": {
                    "base_strength": round(f.base_strength, 2),
                    "close_strength": round(f.close_strength, 2),
                    "sector_strength": round(f.sector_strength, 2),
                    "event_strength": round(f.event_strength, 2),
                    "market_environment": round(f.market_environment, 2),
                },
                "technical": technical,
                "evidence": evidence,
            })

        diagnostics["focus_rows"] = sum(1 for x in rows if x["is_focus"])
        diagnostics["limit_up_rows"] = sum(1 for x in rows if x["is_limit_up"])
        diagnostics["st_rows"] = sum(1 for x in rows if _is_st_name(x["name"]))
        rows.sort(key=lambda x: (not x["is_focus"], x["is_limit_up"], -x["score"], x["code"]))

    if max_candidates is not None:
        rows = rows[:max_candidates]
    return rows, diagnostics


def scan(max_candidates: int | None = None) -> list[dict]:
    rows, _ = scan_with_diagnostics(max_candidates)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="data/daily_candidates.json")
    parser.add_argument("--diagnostics-output", default="data/scan_diagnostics.json")
    parser.add_argument("--max-candidates", type=int, default=None)
    args = parser.parse_args()
    rows, diagnostics = scan_with_diagnostics(args.max_candidates)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    diag_out = Path(args.diagnostics_output)
    diag_out.parent.mkdir(parents=True, exist_ok=True)
    diag_out.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
