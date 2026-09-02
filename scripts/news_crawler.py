"""Lightweight evening news/announcement crawler.

The crawler intentionally uses public HTML pages rather than private APIs. It is a
signal collector, not a trading decision engine: source text is stored first,
then the event layer can score it. Be polite to sites, keep timeouts bounded,
and do not treat a scraped item as verified company disclosure.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


SOURCES = [
    {"name": "财联社电报", "url": "https://m.cls.cn/telegraph", "domain": "cls.cn"},
    {"name": "证券时报", "url": "https://www.stcn.com/", "domain": "stcn.com"},
    {"name": "e公司", "url": "https://egs.stcn.com/news/index", "domain": "stcn.com"},
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "Chrome/128 Safari/537.36"
    )
}


def _parse_page(html: str, base_url: str, source_name: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for tag in soup.find_all(["article", "li", "a"]):
        text = " ".join(tag.get_text(" ", strip=True).split())
        if len(text) < 12 or len(text) > 1000:
            continue
        href = tag.get("href") or ""
        url = urljoin(base_url, href)
        if not url.startswith("http"):
            continue
        title = text[:200]
        key = (title, url)
        if key in seen:
            continue
        seen.add(key)
        code_match = re.search(r"(?<!\d)([036]\d{5})(?:\.(?:SH|SZ))?(?!\d)", text.upper())
        out.append(
            {
                "source": source_name,
                "title": title,
                "url": url,
                "code_hint": code_match.group(1) if code_match else "",
            }
        )
    return out[:300]


def crawl_sources(timeout: int = 12) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    rows: list[dict] = []
    for source in SOURCES:
        try:
            resp = requests.get(source["url"], headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            for row in _parse_page(resp.text, source["url"], source["name"]):
                row["captured_at_utc"] = now
                row["status"] = "ok"
                rows.append(row)
        except requests.RequestException as exc:
            rows.append(
                {
                    "source": source["name"],
                    "status": "error",
                    "error": str(exc),
                    "captured_at_utc": now,
                }
            )
    return rows


def match_candidates(items: list[dict], candidates: list[dict]) -> list[dict]:
    """Match by 6-digit code first, then exact candidate name substring."""
    out: list[dict] = []
    by_code = {str(c.get("code", ""))[:6]: c for c in candidates if c.get("code")}
    for item in items:
        text = item.get("title", "")
        matches: list[dict] = []
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
            item["candidates"] = [
                {"code": m.get("code"), "name": m.get("name")} for m in uniq.values()
            ]
            out.append(item)
    return out


def main() -> None:
    candidates_path = Path("data/daily_candidates.json")
    candidates = json.loads(candidates_path.read_text(encoding="utf-8")) if candidates_path.exists() else []
    raw = crawl_sources()
    matched = match_candidates(raw, candidates)
    out = Path("data/evening_news.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "captured_at_utc": datetime.now(timezone.utc).isoformat(),
                "sources": SOURCES,
                "raw_count": len(raw),
                "matched_count": len(matched),
                "items": matched,
                "source_status": [r for r in raw if r.get("status") == "error"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"crawled={len(raw)} matched={len(matched)}")


if __name__ == "__main__":
    main()
