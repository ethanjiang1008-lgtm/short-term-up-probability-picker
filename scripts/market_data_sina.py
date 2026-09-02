"""新浪公开行情数据适配层。"""
from __future__ import annotations

import json
import ssl
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Referer": "https://finance.sina.com.cn",
}

# 与旧七因子系统一致：普通 TLS 失败时使用宽松 SSL fallback。
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

DEFAULT_TIMEOUT = 15
RETRIES = 3
BACKOFF_SECONDS = (1.0, 2.0, 3.0)
RANK_PAGE_SIZE = 100
MAX_RANK_PAGES = 60
DEFAULT_KLINE_WORKERS = 10


@dataclass(frozen=True)
class SinaStock:
    code: str
    name: str
    market: str


def _fetch_url(url: str, *, timeout: int = DEFAULT_TIMEOUT, label: str = "") -> str:
    """兼容 GitHub Actions 网络环境，复用旧系统的 urllib + SSL fallback。"""
    req = urllib.request.Request(url, headers=HEADERS)
    last_error: Exception | None = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8")
        except Exception as exc:
            last_error = exc
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
                    return resp.read().decode("utf-8")
            except Exception as fallback_exc:
                last_error = fallback_exc
            if attempt < RETRIES - 1:
                time.sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
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
    url = (
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"Market_Center.getHQNodeData?page={page}&num={num}&sort={sort}&asc={asc}"
        f"&node={node}&symbol=&_s_r_a=sort"
    )
    text = _fetch_url(url, timeout=15, label=f"rank:{node}:{sort}:{asc}:page={page}")
    data = json.loads(text)
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
        time.sleep(0.1)
    return rows


def fetch_all_stocks() -> list[dict[str, Any]]:
    return _fetch_all_ranked("changepercent", 0)


def fetch_all_gainers() -> list[dict[str, Any]]:
    return fetch_all_stocks()


def fetch_all_losers() -> list[dict[str, Any]]:
    return _fetch_all_ranked("changepercent", 1)


def _parse_kline_items(data: Any, *, list_style: bool = False) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for item in data:
        try:
            if list_style:
                if not isinstance(item, list) or len(item) < 6:
                    continue
                out.append({
                    "date": item[0],
                    "open": float(item[1] or 0),
                    "close": float(item[2] or 0),
                    "high": float(item[3] or 0),
                    "low": float(item[4] or 0),
                    "volume": float(item[5] or 0),
                    "turnover": 0.0,
                })
            else:
                if not isinstance(item, dict):
                    continue
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


def fetch_kline(code: str, count: int = 80) -> list[dict[str, Any]]:
    """获取日 K 线，使用旧系统已经实际验证过的新浪 JSON 接口。"""
    market = "sh" if code.startswith("6") else "sz"
    symbol = f"{market}{code}"
    # 关键：这里与旧系统保持一致，使用 json_v2.php，而不是 jsonp_v2.php。
    url = (
        "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?"
        f"symbol={symbol}&scale=240&ma=no&datalen={count}"
    )
    return _parse_kline_items(json.loads(_fetch_url(url, timeout=15, label=f"kline:{code}")))


def fetch_klines_parallel(codes: list[str], count: int = 80, workers: int = DEFAULT_KLINE_WORKERS) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
    """批量日K：10线程 + 分批，参考旧系统，避免 24+ 并发造成新浪连接/限流问题。"""
    out: dict[str, list[dict[str, Any]]] = {}
    success = 0
    failed = 0
    if not codes:
        return out, {"requested": 0, "success": 0, "failed": 0}

    batch_size = max(workers * 4, 40)
    for start in range(0, len(codes), batch_size):
        batch = codes[start : start + batch_size]
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(fetch_kline, code, count): code for code in batch}
            for fut in as_completed(futures):
                code = futures[fut]
                try:
                    bars = fut.result()
                    out[code] = bars
                    if bars:
                        success += 1
                    else:
                        failed += 1
                except Exception:
                    out[code] = []
                    failed += 1
        if start + batch_size < len(codes):
            time.sleep(0.5)
    return out, {"requested": len(codes), "success": success, "failed": failed}


if __name__ == "__main__":
    rows = [r for r in fetch_all_stocks() if is_main_board(str(r.get("code", "")), str(r.get("name", "")))]
    print(json.dumps(rows[:20], ensure_ascii=False, indent=2))
