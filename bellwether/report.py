"""报告渲染：rich 终端输出 + markdown 导出 + 免责声明。"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from .agent.prompts import DISCLAIMER
from .models import PortfolioReport

console = Console()


def render_analysis(symbol: str, verdict: str, *, show_disclaimer: bool = True) -> None:
    console.print(
        Panel(Markdown(verdict), title=f"Bellwether · {symbol} 分析", border_style="cyan")
    )
    if show_disclaimer:
        console.print(f"[dim]{DISCLAIMER}[/dim]")


def export_markdown(symbol: str, verdict: str, path: str, *, show_disclaimer: bool = True) -> None:
    """把分析报告写成 markdown 文件。"""
    parts = [f"# Bellwether · {symbol} 分析\n", verdict]
    if show_disclaimer:
        parts.append(f"\n\n---\n\n{DISCLAIMER}")
    Path(path).write_text("\n".join(parts), encoding="utf-8")


def render_portfolio(report: PortfolioReport, *, show_disclaimer: bool = True) -> None:
    summary = (
        f"标的：{'、'.join(report.symbols)}（{report.period}，共同交易日 {report.common_days}）\n"
        f"组合年化波动率：{report.annualized_volatility}%    集中度 HHI：{report.concentration_hhi}"
    )
    console.print(Panel(summary, title="Bellwether · 组合/风险", border_style="cyan"))

    holdings = Table(title="各标的")
    holdings.add_column("标的", style="cyan")
    holdings.add_column("权重", justify="right")
    holdings.add_column("年化收益%", justify="right")
    holdings.add_column("最大回撤%", justify="right")
    for s in report.symbols:
        holdings.add_row(
            s,
            f"{report.weights[s]:.1%}",
            str(report.annualized_returns.get(s)),
            str(report.max_drawdowns.get(s)),
        )
    console.print(holdings)

    matrix = Table(title="相关性矩阵")
    matrix.add_column("", style="cyan")
    for s in report.symbols:
        matrix.add_column(s, justify="right")
    for a in report.symbols:
        matrix.add_row(a, *[f"{report.correlation[a][b]:.2f}" for b in report.symbols])
    console.print(matrix)

    if show_disclaimer:
        console.print(f"[dim]{DISCLAIMER}[/dim]")
