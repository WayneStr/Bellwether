"""report.export_markdown 单测（写临时文件，不打网）。"""

from datetime import UTC

from bellwether.report import export_markdown


def test_export_markdown_with_disclaimer(tmp_path):
    p = tmp_path / "report.md"
    export_markdown("AAPL", "## 结论\n偏强。", str(p), show_disclaimer=True)
    text = p.read_text(encoding="utf-8")
    assert "# Bellwether · AAPL 分析" in text
    assert "偏强" in text
    assert "投资建议" in text  # 免责声明存在


def test_export_markdown_without_disclaimer(tmp_path):
    p = tmp_path / "r.md"
    export_markdown("TEST", "正文内容", str(p), show_disclaimer=False)
    text = p.read_text(encoding="utf-8")
    assert "正文内容" in text
    assert "投资建议" not in text


def test_render_portfolio_runs():
    """渲染不应抛异常（rich 表格 smoke）。"""
    from datetime import datetime

    from bellwether.models import PortfolioReport
    from bellwether.report import render_portfolio

    report = PortfolioReport(
        symbols=["A", "B"],
        weights={"A": 0.5, "B": 0.5},
        period="1y",
        common_days=100,
        correlation={"A": {"A": 1.0, "B": 0.3}, "B": {"A": 0.3, "B": 1.0}},
        annualized_volatility=20.0,
        annualized_returns={"A": 10.0, "B": 5.0},
        max_drawdowns={"A": -15.0, "B": -20.0},
        concentration_hhi=0.5,
        fetched_at=datetime.now(UTC),
    )
    render_portfolio(report, show_disclaimer=False)
