"""新浪公开行情数据适配层。

与 seven-factor-stock-picker 保持一致：先取得足够完整的 hs_a 全市场股票快照，
再由上层做主板、ST、价格等交易宇宙过滤。涨跌榜单仅用于市场情绪统计，不负责定义股票池。
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Referer": "https://finance.sina.com.cn/",
}
DEFAULT_TIMEOUT = (10, 25)
RETRIES = 4
BACKOFF_SECONDS = (1, 2, 4, 8)
RANK_PAGE_SIZE = 100
MAX_RANK_PAGES = 60


@dataclass(frozen=True)
class SinaStock:
    code: str
    name: str
    market: str


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _get_json(url: str, params: dict[str, Any], *, label: str) -> Any:
    last_error: Exception | None = None
    for attempt in range(RETRIES):
        try:
            with _session() as s:
                r = s.get(url, params=params, timeout=DEFAULT_TIMEOUT)
                r.raise_for_status()
                return r.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < RETRIES - 1:
                time.sleep(BACKOFF_SECONDS[attempt])
    raise RuntimeError(f"Sina request failed after {RETRIES} attempts: {label}") from last_error


def _get_text(url: str, params: dict[str, Any], *, label: str) -> str:
    last_error: Exception | None = None
    for attempt in range(RETRIES):
        try:
            with _session() as s:
                r = s.get(url, params=params, timeout=DEFAULT_TIMEOUT)
                r.raise_for_status()
                return r.text
        except requests.RequestException as exc:
            last_error = exc
            if attempt < RETRIES - 1:
                time.sleep(BACKOFF_SECONDS[attempt])
    raise RuntimeError(f"Sina request failed after {RETRIES} attempts: {label}") from last_error


def is_main_board(code: str, name: str = "") -> bool:
    if not code or name.upper().startswith("ST") or "退" in name:
        return False
    return code.startswith(("600", "601", "603", "605", "000", "001", "002", "003"))


def normalize_stock(raw: dict[str, Any]) -> SinaStock | None:
    code = str(raw.get("code", "")).strip()
    name = str(raw.get("name", "")).strip()
    if not code:
        return None
    return SinaStock(code=code, name=name, market="sh" if code.startswith("6") else "sz")


def _normalize_rank_row(item: dict[str, Any]) -> dict[str, Any]:
    def f(key: str, default: float = 0.0) -> float:
        try:
            return float(item.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    return {
        "code": str(item.get("code", "")).strip(),
        "symbol": str(item.get("symbol", "")).strip(),
        "name": str(item.get("name", "")).strip(),
        "price": f("trade"),
        "change_pct": f("changepercent"),
        "change_amt": f("pricechange"),
        "volume": f("volume"),
        "amount": f("amount"),
        "open": f("open"),
        "high": f("high"),
        "low": f("low"),
        "prev_close": f("settlement"),
        "turnover_rate": f("turnoverratio"),
        "total_mcap": f("mktcap"),
        "circ_mcap": f("nmc"),
        "pe": f("per"),
        "pb": f("pb"),
    }


def _fetch_rank_page(node: str, sort: str, asc: int, page: int, num: int = RANK_PAGE_SIZE) -> list[dict[str, Any]]:
    url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
    params = {
        "page": page,
        "num": num,
        "sort": sort,
        "asc": asc,
        "node": node,
        "symbol": "",
        "_s_r_a": "sort",
    }
    data = _get_json(url, params, label=f"rank:{node}:{sort}:{asc}:page={page}")
    if not isinstance(data, list):
        return []
    return [_normalize_rank_row(x) for x in data if isinstance(x, dict)]


def fetch_sina_ranking(node: str = "hs_a", sort: str = "changepercent", asc: int = 0, page: int = 1, num: int = RANK_PAGE_SIZE) -> list[dict[str, Any]]:
    return _fetch_rank_page(node, sort, asc, page, num)


def _fetch_all_ranked(sort: str, asc: int, max_pages: int = MAX_RANK_PAGES) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        batch = _fetch_rank_page("hs_a", sort, asc, page, RANK_PAGE_SIZE)
        if not batch:
            break
        new_count = 0
        for row in batch:
            code = row.get("code", "")
            if code and code not in seen:
                seen.add(code)
                rows.append(row)
                new_count += 1
        if new_count == 0 or len(batch) < RANK_PAGE_SIZE:
            break
        time.sleep(0.15)
    return rows


def fetch_all_stocks() -> list[dict[str, Any]]:
    """获取 hs_a 全市场快照，而不是只获取涨幅榜前几百只。"""
    rows = _fetch_all_ranked("code", 0)
    if not rows:
        # 代码排序入口异常时，用涨幅排序作为可靠回退，并继续翻页到尾部。
        rows = _fetch_all_ranked("changepercent", 0)
    return rows


def fetch_all_gainers() -> list[dict[str, Any]]:
    return _fetch_all_ranked("changepercent", 0)


def fetch_all_losers() -> list[dict[str, Any]]:
    return _fetch_all_ranked("changepercent", 1)


def fetch_kline(code: str, count: int = 240) -> list[dict[str, Any]]:
    """获取日 K 线。"""
    market = "sh" if code.startswith("6") else "sz"
    symbol = f"{market}{code}"
    url = "https://quotes.sina.cn/cn/api/jsonp_v2.php/CN_MarketDataService.getKLineData"
    params = {"symbol": symbol, "scale": 240, "ma": "no", "datalen": count}
    data = _get_json(url, params, label=f"kline:{code}")
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            out.append({
                "date": item.get("day", ""),
                "open": float(item.get("open", 0) or 0),
                "high": float(item.get("high", 0) or 0),
                "low": float(item.get("low", 0) or 0),
                "close": float(item.get("close", 0) or 0),
                "volume": float(item.get("volume", 0) or 0),
                "turnover": float(item.get("turnover", 0) or 0),
            })
        except (TypeError, ValueError):
            continue
    return out


def fetch_klines_parallel(codes: list[str], count: int = 120, workers: int = 12) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fetch_kline, c, count): c for c in codes}
        for fut in as_completed(futures):
            code = futures[fut]
            try:
                out[code] = fut.result()
            except Exception:
                out[code] = []
    return out


if __name__ == "__main__":
    rows = [r for r in fetch_all_stocks() if is_main_board(str(r.get("code", "")), str(r.get("name", "")))]
    print(json.dumps(rows[:20], ensure_ascii=False, indent=2))
