import unittest
from datetime import date

import fund_estimate_demo as demo_entry
import fund_estimate_demo_core as core
from web_app_core import health, index
from web_services import FundDataService


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

    def test_index_and_health_exist(self):
        self.assertEqual(health()["status"], "ok")
        self.assertEqual(index().path.name, "index.html")


if __name__ == "__main__":
    unittest.main()
