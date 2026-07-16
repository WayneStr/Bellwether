"""确定性估值计算。simple_dcf 是纯函数，完全可单测。"""

from __future__ import annotations

# DCF 通用默认假设（非针对个股，仅作粗略参考）
DEFAULT_DCF = {
    "growth_rate": 0.08,
    "discount_rate": 0.09,
    "terminal_growth": 0.025,
    "years": 5,
}


def simple_dcf(
    fcf: float | None,
    shares: float | None,
    *,
    net_debt: float = 0.0,
    **overrides: float,
) -> float | None:
    """两阶段简版 DCF，返回每股内在价值；数据不足或假设非法时返回 None。

    预测 years 年 FCF（按 growth_rate 增长）折现 + Gordon 终值折现 → 企业价值
    → 减净债务 → 除股数。假设见 DEFAULT_DCF，可用关键字覆盖。
    """
    a = {**DEFAULT_DCF, **overrides}
    if not fcf or not shares or shares <= 0:
        return None
    if a["discount_rate"] <= a["terminal_growth"]:
        return None  # 终值公式要求折现率 > 永续增长率

    pv = 0.0
    cf = float(fcf)
    years = int(a["years"])
    for t in range(1, years + 1):
        cf *= 1 + a["growth_rate"]
        pv += cf / (1 + a["discount_rate"]) ** t
    terminal = cf * (1 + a["terminal_growth"]) / (a["discount_rate"] - a["terminal_growth"])
    pv += terminal / (1 + a["discount_rate"]) ** years

    return (pv - net_debt) / shares
