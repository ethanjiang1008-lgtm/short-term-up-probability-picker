"""简单、可解释的尾盘候选扫描器。"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from features import make_features
from market_data_sina import fetch_all_gainers, fetch_all_losers, fetch_klines_parallel, is_main_board


def _num(v: object, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _change_pct(row: dict) -> float:
    return _num(row.get("change_pct", row.get("changepercent", 0.0)))


def _market_stats(gainers: list[dict], losers: list[dict]) -> dict[str, float | int]:
    """只用当前扫描结果估计市场环境，避免再增加一个复杂数据源。"""
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
    }


def _grade(score: float) -> str:
    if score >= 75:
        return "A"
    if score >= 68:
        return "B"
    return "C"


def scan(max_candidates: int = 20) -> list[dict]:
    gainers_raw = fetch_all_gainers()
    losers_raw = fetch_all_losers()
    gainers = [r for r in gainers_raw if is_main_board(str(r.get("code", "")), str(r.get("name", "")))]
    gainers.sort(key=_change_pct, reverse=True)
    stats = _market_stats(gainers_raw, losers_raw)

    codes = [str(r.get("code")) for r in gainers[:150] if r.get("code")]
    klines = fetch_klines_parallel(codes, count=120)

    rows = []
    for r in gainers[:150]:
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
            event_score=0.0,
        )
        if f.score < 65:
            continue
        rows.append({
            "date": str(date.today()),
            "code": f.code,
            "name": f.name,
            "today_close": _num(r.get("trade", r.get("price", 0))),
            "today_change_pct": round(_change_pct(r), 4),
            "score": round(f.score, 2),
            "grade": _grade(f.score),
            "decision": "WATCH" if 65 <= f.score < 75 else "CANDIDATE",
            "probability": None,
            "market": stats,
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
