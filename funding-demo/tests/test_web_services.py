import unittest
from datetime import date, datetime
from unittest.mock import patch

import fund_estimate_demo as demo_entry
import fund_estimate_demo_core as core
from web_app_core import health, index
from web_services import FundDataService, PositionDisclosure


HK_RESPONSE = (
    'var hq_str_hk06869="YOFC,长飞光纤光缆,191.200,197.961,210.600,'
    '190.000,201.200,3.239,1.636,201.00000,201.20000,4009667046,'
    '19806332,0.000,0.000,305.000,18.760,2026/07/03,16:08";'
)
FX_RESPONSE = (
    'var hq_str_fx_shkdcny="03:21:44,0.8646255739,0.8646255739,'
    '0.8657633869,15.5660370000,0.8657633869,0.8658608388,'
    '0.8643042351,0.8646255739,港元兑人民币,-0.1314,-0.0011,'
    '0.0016,此行情由新浪财经计算得出,0.0000,0.0000,,2026-07-04";'
)


class FakeResponse:
    def __init__(self, text):
        self.text = text
        self.encoding = "utf-8"

    def raise_for_status(self):
        return None


class FakeSession:
    headers = {"User-Agent": "test"}

    def get(self, url, **kwargs):
        symbols = url.split("list=", 1)[-1]
        return FakeResponse(FX_RESPONSE if symbols == "fx_shkdcny" else HK_RESPONSE)


class WebServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = FundDataService()
        self.service.session = FakeSession()
        self.holding = core.Holding(
            code="06869",
            name="长飞光纤光缆",
            weight_pct=5.18,
            report_date=date(2026, 3, 31),
            report_label="2026年1季度",
            market="HK",
        )

    def test_hk_quote_parsing(self):
        quote = self.service.fetch_hk_quotes([self.holding])["06869"]
        self.assertAlmostEqual(quote.latest, 201.2)
        self.assertAlmostEqual(quote.previous_close, 197.961)
        self.assertAlmostEqual(quote.change_pct, 1.636)
        self.assertEqual(quote.quote_time.date(), date(2026, 7, 3))

    def test_fx_parsing(self):
        fx = self.service.fetch_hkd_cny()
        self.assertIsNotNone(fx)
        self.assertAlmostEqual(fx.change_pct, -0.1314)
        self.assertAlmostEqual(fx.current, 0.8646255739)

    def test_hk_return_is_converted_to_cny(self):
        original = demo_entry.fetch_a_share_quotes
        demo_entry.fetch_a_share_quotes = lambda session, holdings: {}
        try:
            quotes, metadata = self.service.get_quotes([self.holding])
        finally:
            demo_entry.fetch_a_share_quotes = original
        expected = ((1 + 1.636 / 100) * (1 - 0.1314 / 100) - 1) * 100
        self.assertAlmostEqual(quotes["06869"].change_pct, expected)
        self.assertTrue(metadata["fx_applied"])

    def test_estimate_math(self):
        history = [core.NavPoint(date(2026, 7, 3), 1.2, -1.0)]
        quote = core.Quote("06869", "长飞光纤", 201.2, 197.961, 2.0, self.service.fetch_hk_quotes([self.holding])["06869"].quote_time, "HK")
        result = core.calculate_estimate(history, [self.holding], {"06869": quote}, 0.9)
        self.assertAlmostEqual(result["estimated_change_pct"], 1.8)
        self.assertAlmostEqual(result["estimated_nav"], 1.2216)

    def test_holdings_fall_back_to_another_share_class(self):
        self.service.fund_catalog = lambda: {
            "026211": {"code": "026211", "name": "示例混合C", "type": "混合型"},
            "026210": {"code": "026210", "name": "示例混合A", "type": "混合型"},
        }
        stock = core.Holding("600000", "浦发银行", 5.0)
        with patch.object(
            core,
            "fetch_holdings",
            side_effect=[([], ""), ([stock], "2026年1季度")],
        ) as fetch:
            holdings, metadata = self.service.holdings("026211")

        self.assertEqual(holdings, [stock])
        self.assertEqual(fetch.call_args_list[1].args[1], "026210")
        self.assertEqual(metadata["holding_source_code"], "026210")
        self.assertEqual(metadata["holding_fallback_type"], "share_class")

    def test_etf_feeder_holdings_fall_back_to_target_etf(self):
        self.service.fund_catalog = lambda: {
            "019875": {
                "code": "019875",
                "name": "广发稀有金属ETF联接C",
                "type": "指数型-股票",
            },
            "019874": {
                "code": "019874",
                "name": "广发稀有金属ETF联接A",
                "type": "指数型-股票",
            },
            "159608": {
                "code": "159608",
                "name": "稀有金属ETF广发",
                "type": "指数型-股票",
            },
        }
        stock = core.Holding("600111", "北方稀土", 9.5)

        def fake_fetch(_session, code):
            return ([stock], "2026年1季度") if code == "159608" else ([], "")

        with patch.object(core, "fetch_holdings", side_effect=fake_fetch) as fetch:
            holdings, metadata = self.service.holdings("019875")

        self.assertEqual(holdings, [stock])
        self.assertEqual(
            [call.args[1] for call in fetch.call_args_list],
            ["019875", "019874", "159608"],
        )
        self.assertEqual(metadata["holding_source_code"], "159608")
        self.assertEqual(metadata["holding_fallback_type"], "target_etf")

    def test_quarterly_stock_position_parsing(self):
        html = """
        <table><tr><td>2026-03-31</td><td>82.35%</td><td>1.20%</td>
        <td>8.10%</td><td>12.50</td></tr></table>
        """
        position = self.service._parse_stock_position_html(html)
        self.assertIsNotNone(position)
        self.assertAlmostEqual(position.exposure, 0.8235)
        self.assertEqual(position.report_date, date(2026, 3, 31))
        self.assertEqual(position.position_type, "stock_nav_ratio")

    def test_quarterly_target_etf_position_parsing(self):
        report = """
        广发中证稀有金属主题ETF联接基金 2026年3月31日
        5.2 期末投资目标基金明细
        序号 基金名称 基金类型 运作方式 管理人 公允价值（元） 占基金资产净值比例（%）
        1 广发中证稀有金属主题交易型开放式指数证券投资基金
        股票型 交易型开放式 广发基金管理有限公司 3,330,753,757.31 94.60
        13
        5.3 报告期末按行业分类的股票投资组合
        """
        position = self.service._parse_etf_position_text(report, "159608")
        self.assertIsNotNone(position)
        self.assertAlmostEqual(position.exposure, 0.946)
        self.assertEqual(position.report_date, date(2026, 3, 31))
        self.assertEqual(position.position_type, "target_etf_nav_ratio")

    def test_strategy_classification(self):
        cases = {
            "159608": ("稀有金属ETF广发", "指数型-股票", "etf"),
            "019875": ("广发稀有金属ETF联接C", "指数型-股票", "etf_feeder"),
            "000001": ("示例沪深300指数A", "指数型-股票", "index"),
            "000002": ("示例股票A", "股票型", "equity"),
            "000003": ("示例偏股混合A", "混合型-偏股", "hybrid_equity"),
            "001467": ("华富永鑫灵活配置混合C", "混合型-灵活", "hybrid_flexible"),
        }
        self.service.fund_catalog = lambda: {
            code: {"code": code, "name": name, "type": fund_type}
            for code, (name, fund_type, _) in cases.items()
        }

        for code, (_, _, expected_key) in cases.items():
            with self.subTest(code=code):
                self.assertEqual(self.service.classify_strategy(code).key, expected_key)
    def test_shortened_target_etf_name_is_matched(self):
        self.service.fund_catalog = lambda: {
            "008888": {
                "code": "008888",
                "name": "华夏国证半导体芯片ETF联接C",
                "type": "指数型-股票",
            },
            "008887": {
                "code": "008887",
                "name": "华夏国证半导体芯片ETF联接A",
                "type": "指数型-股票",
            },
            "159995": {
                "code": "159995",
                "name": "芯片ETF华夏",
                "type": "指数型-股票",
            },
            "588200": {
                "code": "588200",
                "name": "科创芯片ETF嘉实",
                "type": "指数型-股票",
            },
        }

        self.assertEqual(self.service._target_etf_code("008888"), "159995")

    def test_etf_feeder_prefers_target_etf_market_quote(self):
        self.service.fund_catalog = lambda: {
            "019875": {
                "code": "019875",
                "name": "广发稀有金属ETF联接C",
                "type": "指数型-股票",
            },
            "019874": {
                "code": "019874",
                "name": "广发稀有金属ETF联接A",
                "type": "指数型-股票",
            },
            "159608": {
                "code": "159608",
                "name": "稀有金属ETF广发",
                "type": "指数型-股票",
            },
        }
        self.service.history = lambda code: [
            core.NavPoint(date(2026, 7, 3), 2.0, -1.0)
        ]
        self.service.position_disclosure = lambda code, strategy, target: (
            PositionDisclosure(
                0.932, date(2026, 3, 31), "quarterly_report_pdf", "target_etf_nav_ratio"
            )
        )
        quote = core.Quote(
            "159608",
            "稀有金属ETF广发",
            1.2,
            1.176,
            2.0,
            datetime(2026, 7, 4, 10, 30),
            "A",
        )

        with patch.object(self.service, "_fetch_etf_quote", return_value=quote):
            with patch.object(core, "fetch_holdings") as fetch_holdings:
                result = self.service.estimate("019875")

        fetch_holdings.assert_not_called()
        self.assertEqual(result["strategy"], "etf_feeder")
        self.assertEqual(result["estimate_source"], "target_etf_market_quote")
        self.assertEqual(result["holding_source_code"], "159608")
        self.assertFalse(result["fallback_used"])
        self.assertAlmostEqual(result["equity_exposure"], 0.932)
        self.assertAlmostEqual(result["estimated_change_pct"], 1.864)
        self.assertAlmostEqual(result["estimated_nav"], 2.0373)
        self.assertEqual(result["position_report_date"], "2026-03-31")
        self.assertEqual(result["position_source"], "quarterly_report_pdf")

    def test_hybrid_strategy_uses_disclosed_position_and_caps_confidence(self):
        self.service.fund_catalog = lambda: {
            "000003": {
                "code": "000003",
                "name": "示例偏股混合A",
                "type": "混合型-偏股",
            }
        }
        self.service.history = lambda code: [
            core.NavPoint(date(2026, 7, 3), 1.0, 0.5)
        ]
        self.service.position_disclosure = lambda code, strategy, target: (
            PositionDisclosure(
                0.68, date(2026, 3, 31), "quarterly_asset_allocation", "stock_nav_ratio"
            )
        )
        holding = core.Holding("600000", "浦发银行", 60.0)
        quote = core.Quote(
            "600000",
            "浦发银行",
            10.2,
            10.0,
            2.0,
            datetime(2026, 7, 4, 10, 30),
            "A",
        )
        self.service.holdings = lambda code: (
            [holding],
            {
                "holding_source_code": code,
                "holding_fallback_type": None,
                "report_label": "2026年1季度",
                "holding_count": 1,
            },
        )
        self.service.get_quotes = lambda holdings: (
            {"600000": quote},
            {
                "a_share_count": 1,
                "hk_share_count": 0,
                "fx_applied": False,
                "hkd_cny_change_pct": None,
                "quote_count": 1,
            },
        )

        result = self.service.estimate("000003")

        self.assertEqual(result["strategy"], "hybrid_equity")
        self.assertEqual(result["estimate_source"], "public_holdings")
        self.assertEqual(result["equity_exposure"], 0.68)
        self.assertEqual(result["confidence"], "中")
        self.assertAlmostEqual(result["estimated_change_pct"], 1.36)
        self.assertEqual(result["position_report_date"], "2026-03-31")

    def test_flexible_hybrid_uses_disclosed_stock_position(self):
        self.service.fund_catalog = lambda: {
            "001467": {
                "code": "001467",
                "name": "华富永鑫灵活配置混合C",
                "type": "混合型-灵活",
            }
        }
        self.service.history = lambda code: [
            core.NavPoint(date(2026, 7, 3), 1.0, 0.5)
        ]
        self.service.position_disclosure = lambda code, strategy, target: (
            PositionDisclosure(
                0.23, date(2026, 3, 31), "quarterly_asset_allocation", "stock_nav_ratio"
            )
        )
        holding = core.Holding("600000", "浦发银行", 20.0)
        quote = core.Quote(
            "600000", "浦发银行", 10.2, 10.0, 2.0,
            datetime(2026, 7, 4, 10, 30), "A",
        )
        self.service.holdings = lambda code: (
            [holding],
            {
                "holding_source_code": code,
                "holding_fallback_type": None,
                "report_label": "2026年1季度",
                "holding_count": 1,
            },
        )
        self.service.get_quotes = lambda holdings: (
            {"600000": quote},
            {
                "a_share_count": 1,
                "hk_share_count": 0,
                "fx_applied": False,
                "hkd_cny_change_pct": None,
                "quote_count": 1,
            },
        )

        result = self.service.estimate("001467")

        self.assertEqual(result["strategy"], "hybrid_flexible")
        self.assertEqual(result["equity_exposure"], 0.23)
        self.assertAlmostEqual(result["estimated_change_pct"], 0.46)
        self.assertEqual(result["confidence"], "中")
        self.assertEqual(result["position_report_date"], "2026-03-31")

    def test_index_strategy_is_explicit_about_holdings_proxy(self):
        self.service.fund_catalog = lambda: {
            "000001": {
                "code": "000001",
                "name": "示例沪深300指数A",
                "type": "指数型-股票",
            }
        }
        self.service.history = lambda code: [
            core.NavPoint(date(2026, 7, 3), 1.0, 0.5)
        ]
        self.service.position_disclosure = lambda code, strategy, target: (
            PositionDisclosure(
                0.97, date(2026, 3, 31), "quarterly_asset_allocation", "stock_nav_ratio"
            )
        )
        holding = core.Holding("600000", "浦发银行", 80.0)
        quote = core.Quote(
            "600000",
            "浦发银行",
            10.1,
            10.0,
            1.0,
            datetime(2026, 7, 4, 10, 30),
            "A",
        )
        self.service.holdings = lambda code: (
            [holding],
            {
                "holding_source_code": code,
                "holding_fallback_type": None,
                "report_label": "2026年1季度",
                "holding_count": 1,
            },
        )
        self.service.get_quotes = lambda holdings: (
            {"600000": quote},
            {
                "a_share_count": 1,
                "hk_share_count": 0,
                "fx_applied": False,
                "hkd_cny_change_pct": None,
                "quote_count": 1,
            },
        )

        result = self.service.estimate("000001")

        self.assertEqual(result["strategy"], "index")
        self.assertEqual(result["estimate_source"], "index_holdings_proxy")
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["equity_exposure"], 0.97)
        self.assertEqual(result["confidence"], "中")

    def test_market_display_mode_uses_china_trading_window(self):
        self.assertEqual(
            self.service.market_display_mode(datetime(2026, 7, 6, 10, 0)),
            "intraday",
        )
        self.assertEqual(
            self.service.market_display_mode(datetime(2026, 7, 6, 15, 0)),
            "official",
        )
        self.assertEqual(
            self.service.market_display_mode(datetime(2026, 7, 5, 10, 0)),
            "official",
        )

    def test_overview_includes_previous_official_nav(self):
        self.service.fund_catalog = lambda: {
            "000001": {"code": "000001", "name": "示例基金", "type": "股票型"}
        }
        self.service.history = lambda code: [
            core.NavPoint(date(2026, 7, 2), 1.0, 0.5),
            core.NavPoint(date(2026, 7, 3), 1.02, None),
        ]
        self.service.market_display_mode = lambda now=None: "official"

        result = self.service.get_overview("000001")

        self.assertEqual(result["latest_nav"], 1.02)
        self.assertEqual(result["previous_nav"], 1.0)
        self.assertEqual(result["previous_nav_date"], "2026-07-02")
        self.assertAlmostEqual(result["latest_daily_return_pct"], 2.0)
        self.assertEqual(result["display_mode"], "official")

    def test_index_and_health_exist(self):
        self.assertEqual(health()["status"], "ok")
        self.assertEqual(index().path.name, "index.html")


if __name__ == "__main__":
    unittest.main()
