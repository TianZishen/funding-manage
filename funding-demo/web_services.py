"""Web Demo 的基金目录、行情、缓存与估值编排。"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import requests

import fund_estimate_demo as demo_entry
import fund_estimate_demo_core as core


CATALOG_URL = "https://fund.eastmoney.com/js/fundcode_search.js"
SINA_QUOTE_URL = "https://hq.sinajs.cn/list={symbols}"


class FundNotFoundError(ValueError):
    pass


class TimedCache:
    def __init__(self) -> None:
        self._items: Dict[str, Tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            item = self._items.get(key)
            if not item:
                return None
            expires_at, value = item
            if expires_at <= time.time():
                self._items.pop(key, None)
                return None
            return value

    def set(self, key: str, value, ttl: int) -> None:
        with self._lock:
            self._items[key] = (time.time() + ttl, value)


@dataclass(frozen=True)
class FxQuote:
    current: float
    previous_close: float
    change_pct: float
    quote_time: datetime


class FundDataService:
    def __init__(self) -> None:
        self.session = core.default_session()
        self.cache = TimedCache()

    @staticmethod
    def normalize_code(code: str) -> str:
        value = str(code).strip()
        if not re.fullmatch(r"\d{6}", value):
            raise FundNotFoundError("基金代码应为6位数字")
        return value

    def fund_catalog(self) -> Dict[str, Dict[str, str]]:
        cached = self.cache.get("fund_catalog")
        if cached is not None:
            return cached
        response = self.session.get(CATALOG_URL, timeout=20)
        response.raise_for_status()
        response.encoding = "utf-8"
        match = re.search(r"=\s*(\[.*\])\s*;?\s*$", response.text, re.S)
        if not match:
            raise ValueError("无法解析基金目录")
        rows = json.loads(match.group(1))
        catalog = {
            str(row[0]): {
                "code": str(row[0]),
                "name": str(row[2]),
                "type": str(row[3]) if len(row) > 3 else "",
                "pinyin": str(row[1]) if len(row) > 1 else "",
            }
            for row in rows
            if len(row) >= 3
        }
        self.cache.set("fund_catalog", catalog, 2 * 60 * 60)
        return catalog

    def validate(self, code: str) -> Dict[str, object]:
        normalized = self.normalize_code(code)
        item = self.fund_catalog().get(normalized)
        if not item:
            return {"exists": False, "code": normalized, "message": "未找到该基金"}
        return {"exists": True, **item}

    def _ensure_fund(self, code: str) -> Dict[str, str]:
        normalized = self.normalize_code(code)
        item = self.fund_catalog().get(normalized)
        if not item:
            raise FundNotFoundError(f"未找到基金 {normalized}")
        return item

    def history(self, code: str) -> List[core.NavPoint]:
        normalized = self.normalize_code(code)
        key = f"history:{normalized}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        self._ensure_fund(normalized)
        result = core.fetch_nav_history(self.session, normalized)
        self.cache.set(key, result, 5 * 60)
        return result

    def history_payload(self, code: str) -> Dict[str, object]:
        normalized = self.normalize_code(code)
        points = self.history(normalized)
        return {
            "code": normalized,
            "count": len(points),
            "points": [
                {
                    "date": item.nav_date.isoformat(),
                    "unit_nav": item.unit_nav,
                    "daily_return_pct": item.daily_return_pct,
                }
                for item in points
            ],
        }

    def get_overview(self, code: str) -> Dict[str, object]:
        normalized = self.normalize_code(code)
        item = self._ensure_fund(normalized)
        history = self.history(normalized)
        latest = history[-1]
        return {
            **item,
            "latest_nav": latest.unit_nav,
            "latest_nav_date": latest.nav_date.isoformat(),
            "latest_daily_return_pct": latest.daily_return_pct,
            "history_count": len(history),
        }

    def holdings(self, code: str) -> Tuple[List[core.Holding], Dict[str, object]]:
        normalized = self.normalize_code(code)
        key = f"holdings:{normalized}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        holdings, label = core.fetch_holdings(self.session, normalized)
        source_code = normalized
        # 026211 是 C 类份额，公开持仓有时只挂在同基金 A 类 026210 下。
        if not holdings and normalized == "026211":
            holdings, label = core.fetch_holdings(self.session, "026210")
            source_code = "026210"
        result = (
            holdings,
            {
                "holding_source_code": source_code,
                "report_label": label,
                "holding_count": len(holdings),
            },
        )
        self.cache.set(key, result, 60 * 60)
        return result

    def fetch_hk_quotes(self, holdings: Sequence[core.Holding]) -> Dict[str, core.Quote]:
        targets = [item for item in holdings if item.market == "HK"]
        if not targets:
            return {}
        symbols = ",".join(f"hk{item.code.zfill(5)}" for item in targets)
        response = self.session.get(
            SINA_QUOTE_URL.format(symbols=symbols),
            headers={"Referer": "https://finance.sina.com.cn/"},
            timeout=10,
        )
        response.raise_for_status()
        response.encoding = "gbk"
        result: Dict[str, core.Quote] = {}
        for symbol, payload in re.findall(r'var hq_str_(hk\d+)="(.*?)";', response.text):
            fields = payload.split(",")
            if len(fields) < 19 or not fields[0]:
                continue
            code = symbol[2:].zfill(5)
            try:
                previous = float(fields[3])
                latest = float(fields[6])
                pct = float(fields[8]) if fields[8] else ((latest / previous - 1) * 100)
                quote_time = datetime.strptime(f"{fields[17]} {fields[18]}", "%Y/%m/%d %H:%M")
            except (ValueError, IndexError):
                continue
            result[code] = core.Quote(
                code=code,
                name=fields[1] or fields[0],
                latest=latest,
                previous_close=previous,
                change_pct=pct,
                quote_time=quote_time,
                market="HK",
            )
        return result

    def fetch_hkd_cny(self) -> Optional[FxQuote]:
        response = self.session.get(
            SINA_QUOTE_URL.format(symbols="fx_shkdcny"),
            headers={"Referer": "https://finance.sina.com.cn/"},
            timeout=10,
        )
        response.raise_for_status()
        response.encoding = "gbk"
        match = re.search(r'var hq_str_fx_shkdcny="(.*?)";', response.text)
        if not match:
            return None
        fields = match.group(1).split(",")
        try:
            current = float(fields[1])
            previous = float(fields[3])
            pct = float(fields[10]) if fields[10] else (current / previous - 1) * 100
            quote_time = datetime.strptime(f"{fields[17]} {fields[0]}", "%Y-%m-%d %H:%M:%S")
        except (ValueError, IndexError):
            return None
        return FxQuote(current=current, previous_close=previous, change_pct=pct, quote_time=quote_time)

    def get_quotes(
        self, holdings: Sequence[core.Holding]
    ) -> Tuple[Dict[str, core.Quote], Dict[str, object]]:
        quotes: Dict[str, core.Quote] = {}
        a_quotes = demo_entry.fetch_a_share_quotes(self.session, holdings)
        quotes.update(a_quotes)
        hk_quotes = self.fetch_hk_quotes(holdings)
        fx = self.fetch_hkd_cny() if hk_quotes else None
        if fx:
            for code, quote in list(hk_quotes.items()):
                cny_change = ((1 + quote.change_pct / 100) * (1 + fx.change_pct / 100) - 1) * 100
                hk_quotes[code] = core.Quote(
                    code=quote.code,
                    name=quote.name,
                    latest=quote.latest,
                    previous_close=quote.previous_close,
                    change_pct=cny_change,
                    quote_time=quote.quote_time,
                    market=quote.market,
                )
        quotes.update(hk_quotes)
        return quotes, {
            "a_share_count": len(a_quotes),
            "hk_share_count": len(hk_quotes),
            "fx_applied": bool(fx),
            "hkd_cny_change_pct": fx.change_pct if fx else None,
            "quote_count": len(quotes),
        }

    def estimate(self, code: str, equity_exposure: float = 0.90) -> Dict[str, object]:
        normalized = self.normalize_code(code)
        self._ensure_fund(normalized)
        cache_key = f"estimate:{normalized}:{equity_exposure:.4f}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        history = self.history(normalized)
        holdings, holding_meta = self.holdings(normalized)
        if not holdings:
            raise ValueError("该基金暂未解析到公开股票持仓")
        quotes, quote_meta = self.get_quotes(holdings)
        result = core.calculate_estimate(history, holdings, quotes, equity_exposure)
        payload = {
            "status": "ok",
            "code": normalized,
            "estimated_at": datetime.now().isoformat(timespec="seconds"),
            **result,
            **holding_meta,
            **quote_meta,
            "disclaimer": "盘中估值基于公开持仓和行情推算，不代表基金公司正式净值。",
        }
        self.cache.set(cache_key, payload, 30)
        return payload


service = FundDataService()
