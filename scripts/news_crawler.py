"""Public news collector for the overnight review window."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

SOURCES = [
    {"name": "财联社电报", "url": "https://m.cls.cn/telegraph", "domain": "cls.cn"},
    {"name": "证券时报", "url": "https://www.stcn.com/", "domain": "stcn.com"},
    {"name": "e公司", "url": "https://egs.stcn.com/news/index", "domain": "stcn.com"},
]
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128 Safari/537.36"}
BEIJING = ZoneInfo("Asia/Shanghai")


def _parse_page(html: str, base_url: str, source_name: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    now_bj = datetime.now(BEIJING)
    for tag in soup.find_all(["article", "li", "a"]):
        text = " ".join(tag.get_text(" ", strip=True).split())
        if len(text) < 12 or len(text) > 1000:
            continue
        url = urljoin(base_url, tag.get("href") or "")
        if not url.startswith("http"):
            continue
        key = (text[:200], url)
        if key in seen:
            continue
        seen.add(key)
        code_match = re.search(r"(?<!\d)([036]\d{5})(?:\.(?:SH|SZ))?(?!\d)", text.upper())
        time_match = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", text)
        published = None
        if time_match:
            hour, minute = map(int, time_match.groups())
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                published = now_bj.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if published > now_bj + timedelta(minutes=5):
                    published -= timedelta(days=1)
        out.append({
            "source": source_name,
            "title": text[:200],
            "url": url,
            "code_hint": code_match.group(1) if code_match else "",
            "published_at_utc": published.astimezone(timezone.utc).isoformat() if published else "",
            "time_quality": "parsed" if published else "unknown",
        })
    return out[:500]


def crawl_sources(timeout: int = 12) -> list[dict]:
    now = datetime.now(timezone.utc)
    rows = []
    for source in SOURCES:
        try:
            resp = requests.get(source["url"], headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            for row in _parse_page(resp.text, source["url"], source["name"]):
                row["captured_at_utc"] = now.isoformat()
                row["status"] = "ok"
                rows.append(row)
        except requests.RequestException as exc:
            rows.append({"source": source["name"], "status": "error", "error": str(exc), "captured_at_utc": now.isoformat()})
    return rows


def crawl_overnight_news(start_utc: datetime, end_utc: datetime, timeout: int = 12) -> list[dict]:
    rows = crawl_sources(timeout)
    out = []
    for row in rows:
        ts = row.get("published_at_utc")
        if not ts:
            if row.get("status") == "ok":
                row["window_status"] = "unknown_time"
                out.append(row)
            continue
        try:
            published = datetime.fromisoformat(ts)
        except ValueError:
            row["window_status"] = "unknown_time"
            out.append(row)
            continue
        if start_utc <= published <= end_utc:
            row["window_status"] = "inside_window"
            out.append(row)
    return out


def match_candidates(items: list[dict], candidates: list[dict]) -> list[dict]:
    out = []
    by_code = {str(c.get("code", ""))[:6]: c for c in candidates if c.get("code")}
    for item in items:
        text = item.get("title", "")
        matches = []
        hinted = item.get("code_hint", "")
        if hinted in by_code:
            matches.append(by_code[hinted])
        for c in candidates:
            name = str(c.get("name", "")).strip()
            if name and len(name) >= 2 and name in text:
                matches.append(c)
        if matches:
            uniq = {str(m.get("code")): m for m in matches}
            item = dict(item)
            item["candidates"] = [{"code": m.get("code"), "name": m.get("name")} for m in uniq.values()]
            out.append(item)
    return out


def main() -> None:
    candidates_path = Path("data/daily_candidates.json")
    candidates = json.loads(candidates_path.read_text(encoding="utf-8")) if candidates_path.exists() else []
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=18)
    raw = crawl_overnight_news(start, now)
    matched = match_candidates(raw, candidates)
    out = Path("data/overnight_news.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "window_start_utc": start.isoformat(),
        "window_end_utc": now.isoformat(),
        "sources": SOURCES,
        "raw_count": len(raw),
        "matched_count": len(matched),
        "items": matched,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"overnight raw={len(raw)} matched={len(matched)}")


if __name__ == "__main__":
    main()
