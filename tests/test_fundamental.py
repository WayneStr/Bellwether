"""FundamentalModule.compute 的单测：比率单位转换 + DCF 触发（假 provider，不打网）。"""

from datetime import datetime, timezone

from bellwether.analysis.fundamental import FundamentalModule
from bellwether.models import FundamentalData


class _FakeProvider:
    source = "fake"

    def get_fundamentals(self, symbol):
        return FundamentalData(
            symbol=symbol,
            roe=0.14,             # 小数比率 → 应转成 14.0
            gross_margins=0.48,   # → 48.0
            pe=30.0,
            free_cashflow=100.0,
            shares_outstanding=100.0,
            fetched_at=datetime.now(timezone.utc),
            source="fake",
        )


def test_ratio_fields_converted_to_pct():
    r = FundamentalModule().compute("TEST", _FakeProvider())
    assert r.metrics["ROE(%)"] == 14.0
    assert r.metrics["毛利率(%)"] == 48.0
    assert r.metrics["PE"] == 30.0  # 非比率字段不受影响


def test_dcf_triggered_when_data_present():
    r = FundamentalModule().compute("TEST", _FakeProvider())
    assert r.dcf_fair_value is not None
    assert r.dcf_note and "参考" in r.dcf_note
