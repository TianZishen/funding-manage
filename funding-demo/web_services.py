"""Web Demo 的基金目录、行情、缓存与估值编排。"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import requests
from bs4 import BeautifulSoup

import fund_estimate_demo as demo_entry
import fund_estimate_demo_core as core


CATALOG_URL = "https://fund.eastmoney.com/js/fundcode_search.js"
ASSET_ALLOCATION_URL = "https://fundf10.eastmoney.com/zcpz_{code}.html"
REPORT_LIST_URL = "https://api.fund.eastmoney.com/f10/JJGG"
REPORT_PDF_URL = "https://pdf.dfcfw.com/pdf/H2_{report_id}_1.pdf"
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


@dataclass(frozen=True)
class EstimateStrategy:
    key: str
    label: str


@dataclass(frozen=True)
class PositionDisclosure:
    exposure: float
    report_date: Optional[date]
    source: str
    position_type: str


ESTIMATE_STRATEGIES = {
    "etf": EstimateStrategy("etf", "ETF场内实时行情"),
    "etf_feeder": EstimateStrategy("etf_feeder", "ETF联接基金"),
    "index": EstimateStrategy("index", "指数基金持仓代理"),
    "equity": EstimateStrategy("equity", "普通股票型基金"),
    "hybrid_equity": EstimateStrategy("hybrid_equity", "偏股混合型基金"),
    "hybrid_flexible": EstimateStrategy("hybrid_flexible", "灵活配置混合型基金"),
}


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

    @staticmethod
    def _compact_fund_name(name: str) -> str:
        return re.sub(r"[\s()（）\-_/]", "", name).upper()

    @staticmethod
    def _is_target_etf_name(target_name: str, candidate_name: str) -> bool:
        if candidate_name == target_name:
            return True
        # Catalog names often move the fund company from prefix to suffix and
        # shorten the index name, e.g. 华夏国证半导体芯片ETF -> 芯片ETF华夏.
        for index in range(2, min(7, len(target_name))):
            issuer = target_name[:index]
            target_theme = target_name[index:]
            if not candidate_name.endswith(issuer):
                continue
            candidate_theme = candidate_name[: -len(issuer)]
            if not target_theme.endswith("ETF") or not candidate_theme.endswith("ETF"):
                continue
            target_core = target_theme[:-3]
            candidate_core = candidate_theme[:-3]
            if len(candidate_core) >= 2 and (
                candidate_core in target_core or target_core in candidate_core
            ):
                return True
        return False

    def _holding_fallback_candidates(self, code: str) -> List[Tuple[str, str]]:
        catalog = self.fund_catalog()
        fund = catalog.get(code)
        if not fund:
            return []

        name = self._compact_fund_name(fund["name"])
        suffix = r"(?:A|B|C|D|E|I|Y|人民币|美元现汇|美元现钞)$"
        share_base = re.sub(suffix, "", name)
        candidates: List[Tuple[str, str]] = []

        # Some share classes publish holdings only under another class.
        for candidate_code, item in catalog.items():
            if candidate_code == code:
                continue
            candidate_name = self._compact_fund_name(item["name"])
            if re.sub(suffix, "", candidate_name) == share_base:
                candidates.append((candidate_code, "share_class"))

        # Feeder funds hold the target ETF rather than its constituent stocks.
        feeder_match = re.fullmatch(r"(.+ETF)联接(?:A|B|C|D|E|I|Y)?", name)
        if feeder_match:
            target_name = feeder_match.group(1)
            for candidate_code, item in catalog.items():
                if candidate_code == code or not candidate_code.startswith(("15", "51", "56", "58")):
                    continue
                candidate_name = self._compact_fund_name(item["name"])
                if "联接" in candidate_name:
                    continue
                if self._is_target_etf_name(target_name, candidate_name):
                    candidate = (candidate_code, "target_etf")
                    if candidate not in candidates:
                        candidates.append(candidate)
        return candidates

    def classify_strategy(self, code: str) -> EstimateStrategy:
        fund = self._ensure_fund(code)
        name = self._compact_fund_name(fund["name"])
        fund_type = self._compact_fund_name(fund.get("type", ""))

        if "ETF联接" in name:
            return ESTIMATE_STRATEGIES["etf_feeder"]
        if "ETF" in name:
            return ESTIMATE_STRATEGIES["etf"]
        if "偏股" in fund_type and "混合" in fund_type:
            return ESTIMATE_STRATEGIES["hybrid_equity"]
        if "灵活" in fund_type and "混合" in fund_type:
            return ESTIMATE_STRATEGIES["hybrid_flexible"]
        if "指数" in fund_type:
            return ESTIMATE_STRATEGIES["index"]
        if "股票" in fund_type:
            return ESTIMATE_STRATEGIES["equity"]
        raise ValueError("当前仅支持ETF、ETF联接、指数型、普通股票型、偏股混合型和灵活配置混合型基金")

    def _target_etf_code(self, code: str) -> Optional[str]:
        for candidate_code, candidate_type in self._holding_fallback_candidates(code):
            if candidate_type == "target_etf":
                return candidate_code
        return None

    @staticmethod
    def _percentage(text: str) -> Optional[float]:
        match = re.search(r"(-?\d+(?:\.\d+)?)%", text.replace("％", "%"))
        return float(match.group(1)) if match else None

    @classmethod
    def _parse_stock_position_html(
        cls, html: str
    ) -> Optional[PositionDisclosure]:
        soup = BeautifulSoup(html, "html.parser")
        for row in soup.select("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all("td")]
            if len(cells) < 2 or not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", cells[0]):
                continue
            stock_pct = cls._percentage(cells[1])
            if stock_pct is None:
                return None
            return PositionDisclosure(
                exposure=stock_pct / 100,
                report_date=datetime.strptime(cells[0], "%Y-%m-%d").date(),
                source="quarterly_asset_allocation",
                position_type="stock_nav_ratio",
            )
        return None

    @staticmethod
    def _parse_etf_position_text(
        text: str, target_code: str
    ) -> Optional[PositionDisclosure]:
        normalized = re.sub(r"[ \t]+", " ", text)
        report_match = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", normalized)
        report_date = (
            date(*(int(value) for value in report_match.groups()))
            if report_match
            else None
        )
        section_match = re.search(
            r"(?:期末投资目标基金明细|基金投资明细)([\s\S]*?)(?=\n\s*5\.\d+\s)",
            normalized,
        )
        if not section_match:
            return None
        section_text = section_match.group(1)
        # The table row ends with fair value followed by NAV ratio. Restricting
        # parsing to this numeric pair prevents a following PDF page number
        # from being mistaken for the position percentage.
        value_ratio_pairs = re.findall(
            r"(\d[\d,]*\.\d{2})\s+(\d{1,3}(?:\.\d+)?)\s*(?:[%％])?",
            section_text,
        )
        if value_ratio_pairs:
            exposure_pct = float(value_ratio_pairs[-1][1])
        else:
            percent_match = re.search(
                rf"{re.escape(target_code)}[\s\S]{{0,300}}?(\d{{1,3}}(?:\.\d+)?)\s*[%％]",
                section_text,
            )
            if not percent_match:
                return None
            exposure_pct = float(percent_match.group(1))
        if not 0 < exposure_pct <= 100:
            return None
        return PositionDisclosure(
            exposure=exposure_pct / 100,
            report_date=report_date,
            source="quarterly_report_pdf",
            position_type="target_etf_nav_ratio",
        )

    def _latest_quarterly_report_text(self, code: str) -> str:
        response = self.session.get(
            REPORT_LIST_URL,
            params={"fundcode": code, "pageIndex": 1, "pageSize": 100, "type": 3},
            headers={"Referer": "https://fundf10.eastmoney.com/"},
            timeout=20,
        )
        response.raise_for_status()
        items = response.json().get("Data") or []
        reports = [
            item
            for item in items
            if "季度报告" in str(item.get("TITLE") or "") and item.get("ID")
        ]
        if not reports:
            raise ValueError("未找到该基金的季度报告")
        report = max(
            reports,
            key=lambda item: str(item.get("PUBLISHDATE") or item.get("TITLE") or ""),
        )
        pdf_response = self.session.get(
            REPORT_PDF_URL.format(report_id=report["ID"]), timeout=30
        )
        pdf_response.raise_for_status()
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ValueError("缺少季报解析依赖 pypdf") from exc
        from io import BytesIO

        reader = PdfReader(BytesIO(pdf_response.content))
        return "\n".join(page.extract_text() or "" for page in reader.pages)

    def position_disclosure(
        self,
        code: str,
        strategy: EstimateStrategy,
        target_etf_code: Optional[str] = None,
    ) -> PositionDisclosure:
        if strategy.key == "etf":
            return PositionDisclosure(1.0, None, "market_quote", "etf_market_return")
        cache_key = f"position:{code}:{strategy.key}:{target_etf_code or ''}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        if strategy.key == "etf_feeder":
            if not target_etf_code:
                raise ValueError("未识别到ETF联接基金的目标ETF")
            report_text = self._latest_quarterly_report_text(code)
            result = self._parse_etf_position_text(report_text, target_etf_code)
        else:
            response = self.session.get(
                ASSET_ALLOCATION_URL.format(code=code), timeout=20
            )
            response.raise_for_status()
            response.encoding = "utf-8"
            result = self._parse_stock_position_html(response.text)
        if result is None:
            raise ValueError("未解析到最新季报仓位，已停止估值以避免使用默认仓位")
        self.cache.set(cache_key, result, 6 * 60 * 60)
        return result

    def _fetch_etf_quote(self, code: str, name: str) -> Optional[core.Quote]:
        holding = core.Holding(code=code, name=name, weight_pct=100.0, market="A")
        return demo_entry.fetch_a_share_quotes(self.session, [holding]).get(code)

    @staticmethod
    def _etf_quote_estimate(
        history: Sequence[core.NavPoint],
        quote: core.Quote,
        exposure: float,
    ) -> Dict[str, object]:
        latest_nav = history[-1]
        estimated_change_pct = quote.change_pct * exposure
        return {
            "estimated_change_pct": round(estimated_change_pct, 4),
            "estimated_nav": round(
                latest_nav.unit_nav * (1 + estimated_change_pct / 100), 4
            ),
            "base_nav": latest_nav.unit_nav,
            "base_nav_date": latest_nav.nav_date.isoformat(),
            "covered_weight_pct": round(exposure * 100, 4),
            "coverage_ratio": 1.0,
            "confidence": "高",
            "equity_exposure": exposure,
            "matched_count": 1,
            "holding_count": 1,
            "contributions": [
                {
                    "code": quote.code,
                    "name": quote.name,
                    "market": quote.market,
                    "weight_pct": round(exposure * 100, 4),
                    "change_pct": round(quote.change_pct, 4),
                    "contribution_pct": round(estimated_change_pct, 4),
                    "quote_time": quote.quote_time.isoformat(timespec="seconds"),
                }
            ],
        }

    def holdings(self, code: str) -> Tuple[List[core.Holding], Dict[str, object]]:
        normalized = self.normalize_code(code)
        key = f"holdings:{normalized}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        holdings, label = core.fetch_holdings(self.session, normalized)
        source_code = normalized
        fallback_type = None
        if not holdings:
            for candidate_code, candidate_type in self._holding_fallback_candidates(normalized):
                holdings, label = core.fetch_holdings(self.session, candidate_code)
                if holdings:
                    source_code = candidate_code
                    fallback_type = candidate_type
                    break
        result = (
            holdings,
            {
                "holding_source_code": source_code,
                "holding_fallback_type": fallback_type,
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

    def estimate(
        self, code: str
    ) -> Dict[str, object]:
        normalized = self.normalize_code(code)
        fund = self._ensure_fund(normalized)
        strategy = self.classify_strategy(normalized)
        target_code = (
            normalized
            if strategy.key == "etf"
            else self._target_etf_code(normalized)
            if strategy.key == "etf_feeder"
            else None
        )
        position = self.position_disclosure(normalized, strategy, target_code)
        exposure = position.exposure
        cache_key = f"estimate:{normalized}:{strategy.key}:{exposure:.4f}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        history = self.history(normalized)
        if strategy.key in ("etf", "etf_feeder"):

            if target_code:
                target = self.fund_catalog().get(target_code, {})
                try:
                    quote = self._fetch_etf_quote(
                        target_code, target.get("name", target_code)
                    )
                except requests.RequestException:
                    quote = None
                if quote:
                    result = self._etf_quote_estimate(history, quote, exposure)
                    payload = {
                        "status": "ok",
                        "code": normalized,
                        "estimated_at": datetime.now().isoformat(timespec="seconds"),
                        **result,
                        "strategy": strategy.key,
                        "strategy_label": strategy.label,
                        "estimate_source": "target_etf_market_quote",
                        "fallback_used": False,
                        "confidence_reason": "直接使用场内ETF实时涨跌，按目标ETF仓位折算。",
                        "position_report_date": (
                            position.report_date.isoformat() if position.report_date else None
                        ),
                        "position_source": position.source,
                        "position_type": position.position_type,
                        "holding_source_code": target_code,
                        "holding_fallback_type": (
                            "target_etf" if strategy.key == "etf_feeder" else None
                        ),
                        "report_label": "",
                        "a_share_count": 1,
                        "hk_share_count": 0,
                        "fx_applied": False,
                        "hkd_cny_change_pct": None,
                        "quote_count": 1,
                        "disclaimer": "盘中估值使用场内ETF行情推算，不代表基金公司正式净值。",
                    }
                    self.cache.set(cache_key, payload, 30)
                    return payload

        holdings, holding_meta = self.holdings(normalized)
        if not holdings:
            raise ValueError("该基金暂未解析到公开股票持仓")
        quotes, quote_meta = self.get_quotes(holdings)
        result = core.calculate_estimate(history, holdings, quotes, exposure)

        fallback_used = strategy.key in ("etf", "etf_feeder", "index")
        if strategy.key in ("hybrid_equity", "hybrid_flexible"):
            if result["confidence"] == "高":
                result["confidence"] = "中"
            confidence_reason = (
                "灵活配置基金季后调仓空间较大，置信度最高为中。"
                if strategy.key == "hybrid_flexible"
                else "偏股混合基金仓位变化较大，置信度最高为中。"
            )
            source = "public_holdings"
        elif strategy.key == "index":
            if result["confidence"] == "高":
                result["confidence"] = "中"
            confidence_reason = "暂未取得稳定的跟踪指数代码，使用公开持仓代理估算。"
            source = "index_holdings_proxy"
        elif strategy.key in ("etf", "etf_feeder"):
            confidence_reason = "ETF实时行情不可用，已降级为目标ETF成分股估算。"
            source = "target_etf_constituents"
        else:
            confidence_reason = "根据公开股票持仓覆盖率评定。"
            source = "public_holdings"

        payload = {
            "status": "ok",
            "code": normalized,
            "estimated_at": datetime.now().isoformat(timespec="seconds"),
            **result,
            **holding_meta,
            **quote_meta,
            "strategy": strategy.key,
            "strategy_label": strategy.label,
            "estimate_source": source,
            "fallback_used": fallback_used,
            "confidence_reason": confidence_reason,
            "position_report_date": (
                position.report_date.isoformat() if position.report_date else None
            ),
            "position_source": position.source,
            "position_type": position.position_type,
            "disclaimer": "盘中估值基于公开持仓和实时行情推算，不代表基金公司正式净值。",
        }
        self.cache.set(cache_key, payload, 30)
        return payload

service = FundDataService()
