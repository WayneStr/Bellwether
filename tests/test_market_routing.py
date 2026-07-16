"""市场识别与路由单测（纯逻辑，不打网、不需要 akshare 装好）。"""

from bellwether.data.base import ProviderRegistry, detect_market


def test_detect_us():
    assert detect_market("AAPL") == "US"
    assert detect_market("msft") == "US"


def test_detect_cn():
    assert detect_market("600519") == "CN"
    assert detect_market("000001") == "CN"
    assert detect_market("600519.SH") == "CN"
    assert detect_market("000001.SZ") == "CN"


def test_detect_hk():
    assert detect_market("00700.HK") == "HK"
    assert detect_market("0700") == "HK"


def test_for_symbol_routes_to_provider():
    # 实例化不触发 akshare import（延迟到取数），故无需 akshare 装好
    assert ProviderRegistry.for_symbol("AAPL").market == "US"
    assert ProviderRegistry.for_symbol("600519").market == "CN"
    assert ProviderRegistry.for_symbol("600519.SH").market == "CN"
