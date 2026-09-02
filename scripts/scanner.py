"""全市场短线候选扫描器。"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from features import make_features
from market_data_sina import (
    fetch_all_gainers,
    fetch_all_losers,
    fetch_all_stocks,
    fetch_klines_parallel,
    is_main_board,
)


PRICE_MAX = 100.0
FLOAT_MCAP_MIN_BILLION = 2.0
FLOAT_MCAP_MAX_BILLION = 300.0


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
    # Sina mktcap/nmc is in 万元; 1 billion yuan = 100,000 万元.
    return _num(row.get("circ_mcap", row.get("nmc", 0.0))) / 100000.0


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
    return True


def _market_stats(gainers: list[dict], losers: list[dict]) -> dict[str, float | int]:
    valid_gainers = [r for r in gainers if is_main_board(str(r.get("code", "")), str(r.get("name", "")))]
    valid_losers = [r for r in losers if is_main_board(str(r.get("code", "")), str(r.get("name", "")))]
    up = sum(_change_pct(r) > 0 for r in valid_gainers)
    down = sum(_change_pct(r) < 0 for r in valid_losers)
    limit_up = sum(_change_pct(r) >= 9.8 for r in valid_gainers)
    limit_down = sum(_change_pct(r) <= -9.8 for r in valid_losers)
    total = up + down
    return {
        "breadth": up / total if total else 0.5,
        "limit_up_count": limit_up,
        "limit_down_count": limit_down,
        "gainers_rows": len(gainers),
        "losers_rows": len(losers),
    }


def _grade(score: float) -> str:
    if score >= 75:
        return "A"
    if score >= 68:
        return "B"
    return "C"


def scan(max_candidates: int = 20) -> list[dict]:
    # 股票宇宙与市场情绪彻底分开：先获取全量 hs_a，再过滤主板/ST/价格/市值。
    universe_raw = fetch_all_stocks()
    gainers_raw = fetch_all_gainers()
    losers_raw = fetch_all_losers()
    universe = [r for r in universe_raw if _eligible(r)]
    stats = _market_stats(gainers_raw, losers_raw)

    # 对整个合格股票宇宙计算历史 K 线特征，不再只覆盖涨幅榜前150。
    codes = [str(r.get("code")) for r in universe if r.get("code")]
    klines = fetch_klines_parallel(codes, count=120)

    rows = []
    for r in universe:
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
            # 事件雷达尚未接入扫描时保持中性；隔夜复核独立处理事件。
            event_score=50.0,
        )
        rows.append({
            "date": str(date.today()),
            "code": f.code,
            "name": f.name,
            "today_close": _price(r),
            "today_change_pct": round(_change_pct(r), 4),
            "score": round(f.score, 2),
            "grade": _grade(f.score),
            "decision": "WATCH" if 65 <= f.score < 75 else ("CANDIDATE" if f.score >= 75 else "NO_TRADE"),
            "probability": None,
            "market": stats,
            "universe": {
                "raw_rows": len(universe_raw),
                "eligible_rows": len(universe),
                "price_max": PRICE_MAX,
                "float_mcap_billion_min": FLOAT_MCAP_MIN_BILLION,
                "float_mcap_billion_max": FLOAT_MCAP_MAX_BILLION,
            },
            "components": {
                "base_strength": round(f.base_strength, 2),
                "close_strength": round(f.close_strength, 2),
                "sector_strength": round(f.sector_strength, 2),
                "event_strength": round(f.event_strength, 2),
                "market_environment": round(f.market_environment, 2),
            },
            "evidence": f.evidence,
        })

    return sorted(rows, key=lambda x: x["score"], reverse=True)[:max_candidates]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="data/daily_candidates.json")
    parser.add_argument("--max-candidates", type=int, default=20)
    args = parser.parse_args()
    rows = scan(args.max_candidates)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
