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


def _derived_volume_ratio(bars: list[dict]) -> float:
    """透明近似量比：最新成交量 / 前5个交易日平均成交量。"""
    volumes = [_num(b.get("volume")) for b in bars]
    if len(volumes) < 6:
        return float("inf")
    avg_prev5 = sum(volumes[-6:-1]) / 5.0
    if avg_prev5 <= 0:
        return float("inf")
    return volumes[-1] / avg_prev5


def _eligible(row: dict) -> bool:
    code = str(row.get("code", ""))
    name = str(row.get("name", ""))
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


def scan_with_diagnostics(max_candidates: int = 20) -> tuple[list[dict], dict]:
    # 第一阶段：全市场排名快照，只做廉价硬过滤。
    universe_raw = fetch_all_stocks()
    base_universe = [r for r in universe_raw if _eligible(r)]
    stats = _market_stats(universe_raw)

    # 不再把5日均线向上作为硬过滤条件。
    # 基础池直接拉120根K线；同一份K线同时计算量比代理和模型特征，避免重复请求。
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
        },
        "volume_ratio_definition": "latest volume / average volume of previous 5 trading days",
        "kline_strategy": "one 120-bar request per price/mcap/turnover eligible stock; no 5MA hard filter",
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
            rows.append({
                "date": str(date.today()),
                "code": f.code,
                "name": f.name,
                "today_close": _price(r),
                "today_change_pct": round(_change_pct(r), 4),
                "turnover_rate_pct": round(_num(r.get("turnover_rate", 0.0)), 4),
                "volume_ratio": r.get("derived_volume_ratio"),
                "ma5_rising": None,
                "score": round(f.score, 2),
                "grade": _grade(f.score),
                "decision": "WATCH" if 65 <= f.score < 75 else ("CANDIDATE" if f.score >= 75 else "NO_TRADE"),
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
                },
                "data_quality": diagnostics,
                "components": {
                    "base_strength": round(f.base_strength, 2),
                    "close_strength": round(f.close_strength, 2),
                    "sector_strength": round(f.sector_strength, 2),
                    "event_strength": round(f.event_strength, 2),
                    "market_environment": round(f.market_environment, 2),
                },
                "evidence": f.evidence,
            })

    return sorted(rows, key=lambda x: x["score"], reverse=True)[:max_candidates], diagnostics


def scan(max_candidates: int = 20) -> list[dict]:
    rows, _ = scan_with_diagnostics(max_candidates)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="data/daily_candidates.json")
    parser.add_argument("--diagnostics-output", default="data/scan_diagnostics.json")
    parser.add_argument("--max-candidates", type=int, default=20)
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
