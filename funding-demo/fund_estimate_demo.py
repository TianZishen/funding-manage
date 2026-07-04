"""026211 命令行 Demo，并为 A 股行情提供新浪降级源。"""

from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Sequence

import requests

import fund_estimate_demo_core as core


def _sina_symbol(code: str) -> str:
    if code.startswith(("5", "6", "9")):
        return f"sh{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    return f"sz{code}"


def fetch_a_share_quotes_sina(
    session: requests.Session, holdings: Sequence[core.Holding]
) -> Dict[str, core.Quote]:
    targets = [item for item in holdings if item.market == "A"]
    if not targets:
        return {}
    symbols = [_sina_symbol(item.code) for item in targets]
    response = session.get(
        "https://hq.sinajs.cn/list=" + ",".join(symbols),
        headers={"Referer": "https://finance.sina.com.cn/"},
        timeout=10,
    )
    response.raise_for_status()
    response.encoding = "gbk"
    result: Dict[str, core.Quote] = {}
    for symbol, payload in re.findall(r'var hq_str_(\w+)="(.*?)";', response.text):
        fields = payload.split(",")
        if len(fields) < 32 or not fields[0]:
            continue
        code = symbol[-6:]
        try:
            previous, latest = float(fields[2]), float(fields[3])
            pct = (latest / previous - 1) * 100 if previous else 0.0
            quote_time = datetime.strptime(f"{fields[30]} {fields[31]}", "%Y-%m-%d %H:%M:%S")
        except (ValueError, IndexError):
            continue
        result[code] = core.Quote(
            code=code,
            name=fields[0],
            latest=latest,
            previous_close=previous,
            change_pct=pct,
            quote_time=quote_time,
            market="A",
        )
    return result


def fetch_a_share_quotes(
    session: requests.Session, holdings: Sequence[core.Holding]
) -> Dict[str, core.Quote]:
    try:
        quotes = core.fetch_a_share_quotes(session, holdings)
        if quotes:
            return quotes
    except (requests.RequestException, ValueError):
        pass
    return fetch_a_share_quotes_sina(session, holdings)


def main() -> None:
    parser = argparse.ArgumentParser(description="基金历史净值与盘中估值 Demo")
    parser.add_argument("--code", default="026211", help="基金代码")
    parser.add_argument("--equity-exposure", type=float, default=0.90, help="股票仓位假设，默认0.90")
    parser.add_argument("--output-dir", type=Path, default=Path("data"))
    args = parser.parse_args()

    session = core.default_session()
    history = core.fetch_nav_history(session, args.code)
    holdings, report_label = core.fetch_holdings(session, args.code)
    if not holdings and args.code == "026211":
        holdings, report_label = core.fetch_holdings(session, "026210")
    quotes = fetch_a_share_quotes(session, holdings)
    estimate = core.calculate_estimate(history, holdings, quotes, args.equity_exposure)
    core.export_demo(history, holdings, estimate, args.output_dir)

    print(f"基金代码：{args.code}")
    print(f"最新净值：{estimate['base_nav']}（{estimate['base_nav_date']}）")
    print(f"持仓报告：{report_label or '未知'}")
    print(f"预测涨跌：{estimate['estimated_change_pct']:+.2f}%")
    print(f"预测净值：{estimate['estimated_nav']:.4f}")
    print(f"行情覆盖：{estimate['covered_weight_pct']:.2f}%")
    print(f"结果已写入：{args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
