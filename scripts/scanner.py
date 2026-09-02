"""尾盘候选扫描器。

第一版不把 score 直接伪装成概率；只有在历史校准器接入后才输出 probability。
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from features import make_features
from market_data_sina import fetch_all_gainers, fetch_klines_parallel, is_main_board


def _num(v: object, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def scan(max_candidates: int = 20) -> list[dict]:
    gainers = [r for r in fetch_all_gainers() if is_main_board(str(r.get("code", "")), str(r.get("name", "")))]
    gainers = sorted(gainers, key=lambda x: _num(x.get("changepercent")), reverse=True)
    codes = [str(r.get("code")) for r in gainers[:150] if r.get("code")]
    klines = fetch_klines_parallel(codes, count=120)

    rows = []
    for r in gainers[:150]:
        code = str(r.get("code", ""))
        if code not in klines or not klines[code]:
            continue
        # 第一版先留出事件、板块与市场环境接口；实际 provider 接入后由事件雷达填充。
        f = make_features(code, str(r.get("name", "")), klines[code],
                          sector_change_pct=0.0,
                          sector_up_ratio=0.5,
                          sector_limit_up_count=0,
                          market_breadth=0.5,
                          market_limit_up_count=0,
                          market_limit_down_count=0,
                          event_score=0.0)
        if f.score < 60:
            continue
        rows.append({
            "date": str(date.today()),
            "code": f.code,
            "name": f.name,
            "score": round(f.score, 2),
            "grade": "A" if f.score >= 75 else "B",
            "probability": None,
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
