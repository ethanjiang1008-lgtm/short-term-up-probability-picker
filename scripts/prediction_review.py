"""Daily frozen prediction snapshots, next-day validation, and Excel reports."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from labels import next_day_labels
from market_data_sina import fetch_klines_parallel
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_daily_snapshot(rows: list[dict]) -> Path:
    day = str(rows[0]["date"])
    out = Path("data/predictions") / f"{day}_tail.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"date": day, "captured_at_utc": datetime.now(timezone.utc).isoformat(), "rows": rows}
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _flat(row: dict) -> dict:
    t, c, e = row.get("technical") or {}, row.get("components") or {}, row.get("evidence") or {}
    return {
        "日期": row.get("date"), "代码": row.get("code"), "名称": row.get("name"), "收盘价": row.get("today_close"),
        "涨幅%": row.get("today_change_pct"), "换手率%": row.get("turnover_rate_pct"), "量比代理": row.get("volume_ratio"),
        "Score": row.get("score"), "等级": row.get("grade"), "观察": row.get("observation"),
        "重点观察": "是" if row.get("is_focus") else "否", "涨停观察": "是" if row.get("is_limit_up") else "否",
        "MA5": t.get("ma5"), "MA10": t.get("ma10"), "MA20": t.get("ma20"), "MA60": t.get("ma60"),
        "MA5向上": "是" if t.get("ma5_rising") else "否", "站上MA20": "是" if t.get("close_above_ma20") else "否",
        "均线多头": "是" if t.get("ma_bull_alignment") else "否", "连续上涨": t.get("consecutive_up_days"),
        "5日收益": e.get("ret_5d"), "20日收益": e.get("ret_20d"), "相对量能": e.get("relative_volume_5d_vs_20d"),
        "尾盘加速": e.get("tail_acceleration"), "基础强度": c.get("base_strength"), "尾盘强度": c.get("close_strength"),
        "板块强度": c.get("sector_strength"), "事件强度": c.get("event_strength"), "市场环境": c.get("market_environment"),
        "判断理由": row.get("focus_reason"),
    }


def _style_sheet(ws, fill="D9EAF7"):
    ws.freeze_panes = "A2"
    if ws.max_row >= 1:
        ws.auto_filter.ref = ws.dimensions
        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor=fill)
            cell.alignment = Alignment(horizontal="center")
    for column_cells in ws.columns:
        letter = column_cells[0].column_letter
        max_len = min(32, max(len(str(c.value or "")) for c in column_cells) + 2)
        ws.column_dimensions[letter].width = max_len


def export_excel(rows: list[dict], day: str) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "尾盘预测"
    flat_rows = [_flat(r) for r in rows]
    if flat_rows:
        headers = list(flat_rows[0].keys())
        ws.append(headers)
        for item in flat_rows:
            ws.append([item.get(h) for h in headers])
    _style_sheet(ws)
    out = Path("reports") / f"{day}_tail_prediction.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


def _bootstrap_existing_daily_candidates() -> None:
    pred_dir = Path("data/predictions")
    pred_dir.mkdir(parents=True, exist_ok=True)
    if list(pred_dir.glob("*_tail.json")):
        return
    path = Path("data/daily_candidates.json")
    rows = load_json(path)
    if isinstance(rows, list) and rows:
        save_daily_snapshot(rows)
        day = str(rows[0].get("date"))
        if day:
            export_excel(rows, day)
        print(f"bootstrapped_prediction_date={day} rows={len(rows)}")


def validate_previous_day() -> tuple[str | None, list[dict]]:
    _bootstrap_existing_daily_candidates()
    files = sorted(Path("data/predictions").glob("*_tail.json"))
    if not files:
        return None, []
    latest = load_json(files[-1]) or {}
    day = str(latest.get("date", ""))
    rows = latest.get("rows") or []
    if not day or not rows:
        return None, []

    codes = [str(r.get("code")) for r in rows if r.get("code")]
    bars_map, _ = fetch_klines_parallel(codes, count=10, workers=10)
    results = []
    for r in rows:
        code = str(r.get("code"))
        future = []
        for bar in bars_map.get(code, []):
            d = str(bar.get("date", bar.get("day", "")))[:10]
            try:
                close = float(bar.get("close", bar.get("c")))
            except (TypeError, ValueError):
                continue
            if d > day:
                future.append((d, close))
        if not future or not r.get("today_close"):
            continue
        next_day, next_close = sorted(future)[0]
        labels = next_day_labels(float(r["today_close"]), next_close)
        results.append({
            "预测日期": day, "验证日期": next_day, "代码": code, "名称": r.get("name"), "Score": r.get("score"),
            "观察": r.get("observation"), "重点观察": "是" if r.get("is_focus") else "否", "预测等级": r.get("grade"),
            "昨日收盘": r.get("today_close"), "次日收盘": next_close,
            "次日收益%": round(labels.get("next_day_return", 0) * 100, 3),
            "次日上涨": "是" if labels.get("next_day_up") else "否",
            "次日≥3%": "是" if labels.get("next_day_strong_up") else "否",
            "次日涨停": "是" if labels.get("next_day_limit_up") else "否",
        })
    return day, results


def save_validation(day: str, results: list[dict]) -> Path:
    out = Path("data/validation") / f"{day}_validation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def append_validation_to_excel(day: str, results: list[dict]) -> Path:
    path = Path("reports") / f"{day}_tail_prediction.xlsx"
    if not path.exists() or not results:
        return path
    wb = load_workbook(path)
    if "次日验证" in wb.sheetnames:
        del wb["次日验证"]
    ws = wb.create_sheet("次日验证")
    headers = list(results[0].keys())
    ws.append(headers)
    for item in results:
        ws.append([item.get(h) for h in headers])
    _style_sheet(ws, "E2F0D9")
    wb.save(path)
    return path


def main_validate() -> None:
    day, results = validate_previous_day()
    if day:
        save_validation(day, results)
        append_validation_to_excel(day, results)
        print(f"validated_prediction_date={day} rows={len(results)}")
    else:
        print("validated_prediction_date=none rows=0")


if __name__ == "__main__":
    main_validate()
