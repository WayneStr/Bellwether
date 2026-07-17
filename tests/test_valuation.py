"""简版 DCF 的确定性单测：含手算对拍。"""

import math

from bellwether.analysis.valuation import simple_dcf


def test_dcf_none_on_missing_data():
    assert simple_dcf(None, 1000) is None
    assert simple_dcf(1e9, None) is None
    assert simple_dcf(1e9, 0) is None


def test_dcf_rejects_bad_assumptions():
    # 折现率 <= 永续增长率 → 无解
    assert simple_dcf(1e9, 1000, discount_rate=0.02, terminal_growth=0.03) is None


def test_dcf_manual_value():
    disc = 0.10
    pv_flows = sum(100 / (1 + disc) ** t for t in range(1, 6))
    pv_terminal = (100 / disc) / (1 + disc) ** 5
    expected = (pv_flows + pv_terminal) / 100  # net_debt=0, shares=100
    v = simple_dcf(100.0, 100.0, growth_rate=0.0, discount_rate=0.10, terminal_growth=0.0, years=5)
    assert v is not None and math.isclose(v, expected, rel_tol=1e-9)


def test_dcf_net_debt_reduces_value():
    base = simple_dcf(100.0, 100.0)
    with_debt = simple_dcf(100.0, 100.0, net_debt=500.0)
    assert base is not None and with_debt is not None and with_debt < base
