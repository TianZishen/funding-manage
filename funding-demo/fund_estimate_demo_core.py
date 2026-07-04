"""基金历史净值、公开持仓、A 股行情与估值计算核心。"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import requests
from bs4 import BeautifulSoup


FUND_HISTORY_URL = "https://fund.eastmoney.com/pingzhongdata/{code}.js"
FUND_HOLDINGS_URL = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
EASTMONEY_QUOTE_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"


@dataclass(frozen=True)
class NavPoint:
    nav_date: date
    unit_nav: float
    daily_return_pct: Optional[float] = None


@dataclass(frozen=True)
class Holding:
    code: str
    name: str
    weight_pct: float
    report_date: Optional[date] = None
    report_label: str = ""
    market: str = "A"


@dataclass(frozen=True)
class Quote:
    code: str
    name: str
    latest: float
    previous_close: float
    change_pct: float
    quote_time: datetime
    market: str = "A"


def default_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
            ),
            "Referer": "https://fund.eastmoney.com/",
        }
    )
    return session


def _extract_js_json(text: str, variable: str):
    match = re.search(rf"(?:var\s+)?{re.escape(variable)}\s*=\s*(\[.*?\])\s*;", text, re.S)
    if not match:
        raise ValueError(f"数据源未返回 {variable}")
    return json.loads(match.group(1))


def fetch_nav_history(session: requests.Session, code: str) -> List[NavPoint]:
    response = session.get(FUND_HISTORY_URL.format(code=code), timeout=15)
    response.raise_for_status()
    response.encoding = "utf-8"
    rows = _extract_js_json(response.text, "Data_netWorthTrend")
    result: List[NavPoint] = []
    for row in rows:
        if row.get("y") is None:
            continue
        nav_date = datetime.fromtimestamp(int(row["x"]) / 1000).date()
        daily = row.get("equityReturn")
        result.append(
            NavPoint(
                nav_date=nav_date,
                unit_nav=float(row["y"]),
                daily_return_pct=float(daily) if daily is not None else None,
            )
        )
    if not result:
        raise ValueError(f"基金 {code} 没有可用的历史净值")
    return result


def _parse_report_date(text: str) -> Optional[date]:
    match = re.search(r"(20\d{2})[-年](\d{1,2})[-月](\d{1,2})", text)
    if match:
        return date(*(int(value) for value in match.groups()))
    return None


def fetch_holdings(session: requests.Session, code: str) -> Tuple[List[Holding], str]:
    response = session.get(
        FUND_HOLDINGS_URL,
        params={"type": "jjcc", "code": code, "topline": 10, "year": "", "month": ""},
        timeout=15,
    )
    response.raise_for_status()
    response.encoding = "utf-8"
    text = response.text
    html_match = re.search(r'content:"(.*?)",arryear', text, re.S)
    html = html_match.group(1) if html_match else text
    html = html.replace(r'\"', '"').replace(r"\/", "/").replace(r"\n", "")
    soup = BeautifulSoup(html, "html.parser")
    report_label = ""
    title = soup.find(["h4", "label"])
    if title:
        report_label = title.get_text(" ", strip=True)
    report_date = _parse_report_date(report_label)

    holdings: List[Holding] = []
    for row in soup.select("tbody tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all("td")]
        if len(cells) < 4:
            continue
        joined = " ".join(cells)
        code_match = re.search(r"(?<!\d)(\d{5,6})(?!\d)", joined)
        weight_match = next((re.search(r"(-?\d+(?:\.\d+)?)%", value) for value in cells if "%" in value), None)
        if not code_match or not weight_match:
            continue
        stock_code = code_match.group(1)
        link = row.find("a", href=True)
        href = (link.get("href") or "").lower() if link else ""
        market = "HK" if len(stock_code) == 5 or "/hk" in href or "hk." in href else "A"
        name = ""
        for cell in cells:
            if cell != stock_code and "%" not in cell and not cell.isdigit():
                name = cell
                break
        holdings.append(
            Holding(
                code=stock_code.zfill(5) if market == "HK" else stock_code.zfill(6),
                name=name or stock_code,
                weight_pct=float(weight_match.group(1)),
                report_date=report_date,
                report_label=report_label,
                market=market,
            )
        )
    return holdings, report_label


def _a_secid(code: str) -> str:
    return f"1.{code}" if code.startswith(("5", "6", "9")) else f"0.{code}"


def fetch_a_share_quotes(
    session: requests.Session, holdings: Sequence[Holding]
) -> Dict[str, Quote]:
    targets = [holding for holding in holdings if holding.market == "A"]
    if not targets:
        return {}
    response = session.get(
        EASTMONEY_QUOTE_URL,
        params={
            "fltt": 2,
            "secids": ",".join(_a_secid(item.code) for item in targets),
            "fields": "f12,f14,f2,f3,f18,f124",
        },
        timeout=10,
    )
    response.raise_for_status()
    rows = (response.json().get("data") or {}).get("diff") or []
    result: Dict[str, Quote] = {}
    for row in rows:
        code = str(row.get("f12") or "").zfill(6)
        latest, previous, pct = row.get("f2"), row.get("f18"), row.get("f3")
        if not code or latest in (None, "-") or pct in (None, "-"):
            continue
        timestamp = row.get("f124")
        quote_time = datetime.fromtimestamp(timestamp) if timestamp else datetime.now()
        result[code] = Quote(
            code=code,
            name=str(row.get("f14") or code),
            latest=float(latest),
            previous_close=float(previous or 0),
            change_pct=float(pct),
            quote_time=quote_time,
            market="A",
        )
    return result


def calculate_estimate(
    history: Sequence[NavPoint],
    holdings: Sequence[Holding],
    quotes: Dict[str, Quote],
    equity_exposure: float = 0.90,
) -> Dict[str, object]:
    if not history:
        raise ValueError("缺少历史净值")
    matched = [(holding, quotes[holding.code]) for holding in holdings if holding.code in quotes]
    covered_weight = sum(holding.weight_pct for holding, _ in matched)
    if covered_weight <= 0:
        raise ValueError("公开持仓没有匹配到可用行情")

    weighted_sum = sum(
        holding.weight_pct * quote.change_pct / 100 for holding, quote in matched
    )
    average_stock_return = weighted_sum / covered_weight * 100
    estimated_change_pct = average_stock_return * equity_exposure
    latest_nav = history[-1]
    estimated_nav = latest_nav.unit_nav * (1 + estimated_change_pct / 100)
    coverage = min(covered_weight / (equity_exposure * 100), 1.0)
    confidence = "高" if coverage >= 0.65 else "中" if coverage >= 0.40 else "低"

    contributions = []
    for holding, quote in matched:
        normalized_contribution = (
            holding.weight_pct / covered_weight * estimated_change_pct
        )
        contributions.append(
            {
                "code": holding.code,
                "name": holding.name or quote.name,
                "market": holding.market,
                "weight_pct": round(holding.weight_pct, 4),
                "change_pct": round(quote.change_pct, 4),
                "contribution_pct": round(normalized_contribution, 4),
                "quote_time": quote.quote_time.isoformat(timespec="seconds"),
            }
        )
    contributions.sort(key=lambda item: abs(item["contribution_pct"]), reverse=True)

    return {
        "estimated_change_pct": round(estimated_change_pct, 4),
        "estimated_nav": round(estimated_nav, 4),
        "base_nav": latest_nav.unit_nav,
        "base_nav_date": latest_nav.nav_date.isoformat(),
        "covered_weight_pct": round(covered_weight, 4),
        "coverage_ratio": round(coverage, 4),
        "confidence": confidence,
        "equity_exposure": equity_exposure,
        "matched_count": len(matched),
        "holding_count": len(holdings),
        "contributions": contributions,
    }


def export_demo(
    history: Sequence[NavPoint],
    holdings: Sequence[Holding],
    estimate: Dict[str, object],
    output_dir: Path,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "026211_nav_history.csv").open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=["date", "unit_nav", "daily_return_pct"])
        writer.writeheader()
        for item in history:
            writer.writerow(
                {
                    "date": item.nav_date.isoformat(),
                    "unit_nav": item.unit_nav,
                    "daily_return_pct": item.daily_return_pct,
                }
            )
    with (output_dir / "026211_latest_holdings.csv").open("w", newline="", encoding="utf-8-sig") as file:
        rows = [asdict(item) for item in holdings]
        writer = csv.DictWriter(file, fieldnames=list(rows[0]) if rows else ["code"])
        writer.writeheader()
        writer.writerows(rows)
    with (output_dir / "026211_estimate_snapshot.json").open("w", encoding="utf-8") as file:
        json.dump(estimate, file, ensure_ascii=False, indent=2, default=str)
