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


# ─────────── resolve_symbol 市场归一化（2026-07-26 cassette smoke 踩坑回归） ───────────
def test_resolve_symbol_normalizes_market_suffixes():
    """LLM 自由形态代码（00016.HK / 16.HK / 600519.SS）必须收敛到录制键形态——
    live 与 cassette 重放共用此规则，否则 cassette 全量 miss。"""
    from datetime import UTC, datetime

    from bellwether.core.context import AnalysisContext, FrozenClock
    from bellwether.data.yfinance_provider import YFinanceProvider

    now = datetime(2026, 7, 26, tzinfo=UTC)
    ctx = AnalysisContext(as_of=now, capture_policy="live", clock=FrozenClock(now))
    p = YFinanceProvider()  # 归一化在基类默认实现，任一 provider 行为一致
    cases = {
        "00016.HK": "00016",
        "16.HK": "00016",
        "600519.SS": "600519",
        "600519.SZ": "600519",
        "9988": "09988",
        "00700": "00700",
        "AAPL": "AAPL",
        "brk-b": "BRK-B",
    }
    for query, want in cases.items():
        assert p.resolve_symbol(query, context=ctx) == want, query
