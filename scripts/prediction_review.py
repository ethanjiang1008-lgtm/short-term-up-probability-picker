"""Daily prediction snapshot, next-day validation, and Excel export."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from market_data_sina import fetch_klines_parallel
from labels import next_day_labels
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment


def load_json(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
    except (OSError, json.JSONDecodeError):
        return None


def save_daily_snapshot(rows: list[dict]) -> Path:
    day = str(rows[0]["date"])
    out = Path("data/predictions") / f"{day}_tail.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": day,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "rows": rows,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _flat(row: dict) -> dict:
    t = row.get("technical") or {}
    c = row.get("components") or {}
    e = row.get("evidence") or {}
    return {
        "日期": row.get("date"), "代码": row.get("code"), "名称": row.get("name"),
        "收盘价": row.get("today_close"), "涨幅%": row.get("today_change_pct"),
        "换手率%": row.get("turnover_rate_pct"), "量比代理": row.get("volume_ratio"),
        "Score": row.get("score"), "等级": row.get("grade"), "观察": row.get("observation"),
        "重点观察": "是" if row.get("is_focus") else "否", "涨停观察": "是" if row.get("is_limit_up") else "否",
        "MA5": t.get("ma5"), "MA10": t.get("ma10"), "MA20": t.get("ma20"), "MA60": t.get("ma60"),
        "MA5向上": "是" if t.get("ma5_rising") else "否", "站上MA20": "是" if t.get("close_above_ma20") else "否",
        "均线多头": "是" if t.get("ma_bull_alignment") else "否", "连续上涨": t.get("consecutive_up_days"),
        "5日收益": e.get("ret_5d"), "20日收益": e.get("ret_20d"),
        "相对量能": e.get("relative_volume_5d_vs_20d"), "尾盘加速": e.get("tail_acceleration"),
        "基础强度": c.get("base_strength"), "尾盘强度": c.get("close_strength"),
        "板块强度": c.get("sector_strength"), "事件强度": c.get("event_strength"),
        "市场环境": c.get("market_environment"), "判断理由": row.get("focus_reason"),
    }


def export_excel(rows: list[dict], day: str, validation_rows: list[dict] | None = None) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "尾盘预测"
    flat_rows = [_flat(r) for r in rows]
    if flat_rows:
        headers = list(flat_rows[0].keys())
        ws.append(headers)
        for item in flat_rows:
            ws.append([item.get(h) for h in headers])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="D9EAF7")
        cell.alignment = Alignment(horizontal="center")

    if validation_rows:
        wv = wb.create_sheet("次日验证")
        headers = list(validation_rows[0].keys())
        wv.append(headers)
        for item in validation_rows:
            wv.append([item.get(h) for h in headers])
        wv.freeze_panes = "A2"
        wv.auto_filter.ref = wv.dimensions
        for cell in wv[1]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="E2F0D9")
            cell.alignment = Alignment(horizontal="center")

    for sheet in wb.worksheets:
        for column_cells in sheet.columns:
            letter = column_cells[0].column_letter
            max_len = min(32, max(len(str(c.value or "")) for c in column_cells) + 2)
            sheet.column_dimensions[letter].width = max_len

    out = Path("reports") / f"{day}_tail_prediction.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


def validate_previous_day() -> list[dict]:
    pred_dir = Path("data/predictions")
    files = sorted(pred_dir.glob("*_tail.json"))
    if not files:
        return []
    latest = load_json(files[-1]) or {}
    day = latest.get("date")
    rows = latest.get("rows") or []
    if not day or not rows:
        return []
    targets = [r for r in rows if r.get("next_day_close") is None]
    codes = [str(r.get("code")) for r in targets if r.get("code")]
    bars_map, _ = fetch_klines_parallel(codes, count=10, workers=10)
    results = []
    for r in targets:
        code = str(r.get("code"))
        future = []
        for bar in bars_map.get(code, []):
            d = str(bar.get("date", bar.get("day", "")))[:10]
            try: close = float(bar.get("close", bar.get("c")))
            except (TypeError, ValueError): continue
            if d > str(day): future.append((d, close))
        if not future or not r.get("today_close"):
            continue
        next_day, next_close = sorted(future)[0]
        try: labels = next_day_labels(float(r["today_close"]), next_close)
        except (TypeError, ValueError): continue
        item = {"预测日期": day, "验证日期": next_day, "代码": code, "名称": r.get("name"),
                "Score": r.get("score"), "观察": r.get("observation"), "重点观察": "是" if r.get("is_focus") else "否",
                "昨日收盘": r.get("today_close"), "次日收盘": next_close,
                "次日收益%": round(labels.get("next_day_return", 0) * 100, 3),
                "次日上涨": "是" if labels.get("next_day_up") else "否",
                "次日≥3%": "是" if labels.get("next_day_strong_up") else "否",
                "次日涨停": "是" if labels.get("next_day_limit_up") else "否"}
        results.append(item)
    return results


if __name__ == "__main__":
    rows = load_json(Path("data/daily_candidates.json")) or []
    if rows:
        save_daily_snapshot(rows)
        validation = validate_previous_day()
        day = str(rows[0]["date"])
        print(f"saved_prediction={day} rows={len(rows)} validated={len(validation)}")
        print(f"excel={export_excel(rows, day, validation)}")
