"""新浪公开行情数据适配层。

尽量保持与 seven-factor-stock-picker 的数据获取思路一致，但将请求逻辑
独立成可复用模块，方便未来替换/增加其他 provider。
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

UA_HEADERS = {"User-Agent": HEADERS["User-Agent"]}
DEFAULT_TIMEOUT = 12


@dataclass(frozen=True)
class SinaStock:
    code: str
    name: str
    market: str


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


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


def fetch_all_gainers(limit: int = 500) -> list[dict[str, Any]]:
    """抓取新浪涨幅排行。

    新浪接口字段可能随时间变化，因此这里保持宽松解析，未知字段原样保留。
    """
    url = "https://quotes.sina.cn/cn/api/openapi.php/Market_Center.getHQNodeData"
    params = {"page": 1, "num": limit, "sort": "changepercent", "asc": 0, "node": "hs_a"}
    with _session() as s:
        r = s.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        return r.json().get("result", {}).get("data", [])


def fetch_all_losers(limit: int = 500) -> list[dict[str, Any]]:
    url = "https://quotes.sina.cn/cn/api/openapi.php/Market_Center.getHQNodeData"
    params = {"page": 1, "num": limit, "sort": "changepercent", "asc": 1, "node": "hs_a"}
    with _session() as s:
        r = s.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        return r.json().get("result", {}).get("data", [])


def fetch_kline(code: str, count: int = 240) -> list[dict[str, Any]]:
    """获取日 K 线；新浪接口使用 sh/sz 前缀。"""
    market = "sh" if code.startswith("6") else "sz"
    symbol = f"{market}{code}"
    url = "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_%3D/cn/klga/{symbol}/daily"
    params = {"datalen": count}
    with _session() as s:
        r = s.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        text = r.text
    # 兼容 var _=([...]); 形式
    m = re.search(r"\((\[.*\])\)", text, re.S)
    if not m:
        return []
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return []


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
            time.sleep(0.01)
    return out


if __name__ == "__main__":
    rows = [r for r in fetch_all_gainers() if is_main_board(str(r.get("code", "")), str(r.get("name", "")))]
    print(json.dumps(rows[:20], ensure_ascii=False, indent=2))
