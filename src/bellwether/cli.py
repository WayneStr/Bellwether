"""Bellwether CLI 入口（typer）。"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from .agent.router import VALID_ROLES, ModelRouter
from .config import KEYRING_SERVICE, KEYRING_USERNAME, api_key_source, load_config
from .core.exceptions import BellwetherError
from .core.redact import redact

app = typer.Typer(
    add_completion=False,
    help="Bellwether — 股票分析与规划 AI Agent（研究辅助，非投资建议）",
)
config_app = typer.Typer(help="配置相关命令")
app.add_typer(config_app, name="config")

console = Console()


@app.command()
def analyze(
    symbol: str = typer.Argument(..., help="股票代码，如 AAPL"),
    deep: bool = typer.Option(False, "--deep", help="生成深度报告（走 deep_report 角色模型）"),
    model: str | None = typer.Option(None, "--model", help="覆盖本次使用的模型 id"),
    temperature: float | None = typer.Option(None, "--temperature", help="覆盖采样温度"),
    max_tokens: int | None = typer.Option(None, "--max-tokens", help="覆盖最大输出 tokens"),
    output: str | None = typer.Option(None, "--output", "-o", help="把报告导出为 markdown 文件"),
    config_path: str | None = typer.Option(None, "--config", help="指定 config.toml 路径"),
) -> None:
    """分析单只股票。"""
    config = load_config(config_path)
    if not config.anthropic_api_key:
        console.print("[red]未检测到 ANTHROPIC_API_KEY 环境变量[/red]，无法调用模型。")
        console.print("请先设置后重试，例如：[dim]export ANTHROPIC_API_KEY=sk-...[/dim]")
        raise typer.Exit(code=1)

    # 延迟 import：无 key 时不必加载 anthropic/yfinance
    from .agent.orchestrator import Orchestrator
    from .report import render_analysis

    overrides: dict = {}
    if temperature is not None:
        overrides["temperature"] = temperature
    if max_tokens is not None:
        overrides["max_tokens"] = max_tokens

    orch = Orchestrator(config)
    try:
        with console.status(f"正在分析 {symbol} ……"):
            verdict = orch.analyze(symbol, deep=deep, model_override=model, **overrides)
    except BellwetherError as exc:  # 重试与降级仍未成功 → 明示失败（D2），不落半截报告
        console.print(f"[red]分析失败[/red]（{type(exc).__name__}）：{redact(str(exc))}")
        console.print("[dim]已按类型重试/降级仍失败；可稍后重试，或用 --model 指定其他模型。[/dim]")
        if orch.last_trace_path:
            console.print(f"[dim]故障 trace：{orch.last_trace_path}[/dim]")
        raise typer.Exit(code=1) from exc

    render_analysis(symbol, verdict, show_disclaimer=config.report.disclaimer)
    if orch.last_trace_path:
        console.print(f"[dim]分析溯源已记录：{orch.last_trace_path}[/dim]")
    if output:
        from .report import export_markdown

        export_markdown(symbol, verdict, output, show_disclaimer=config.report.disclaimer)
        console.print(f"[green]已导出[/green] → {output}")


@config_app.command("show")
def config_show(
    config_path: str | None = typer.Option(None, "--config", help="指定 config.toml 路径"),
) -> None:
    """显示当前生效的模型配置（用于验证「模型可配置」是否按预期解析）。"""
    config = load_config(config_path)
    router = ModelRouter(config.models)

    table = Table(title="Bellwether 生效模型配置")
    table.add_column("角色 role", style="cyan")
    table.add_column("模型 model")
    table.add_column("temperature", justify="right")
    table.add_column("max_tokens", justify="right")
    for role in VALID_ROLES:
        spec = router.resolve(role)
        table.add_row(role, spec.model, str(spec.params.temperature), str(spec.params.max_tokens))
    console.print(table)

    source = api_key_source()
    key_state = {"env": "已设置（环境变量）", "keyring": "已设置（系统钥匙串）"}.get(
        source, "[red]未设置[/red]"
    )
    console.print(f"ANTHROPIC_API_KEY：{key_state}")
    console.print(
        f"API 请求地址：{config.anthropic_base_url or '官方默认 https://api.anthropic.com'}"
    )


@config_app.command("set-key")
def config_set_key() -> None:
    """把 API key 存入系统钥匙串（keyring）。环境变量 ANTHROPIC_API_KEY 始终优先。"""
    try:
        import keyring
    except ImportError as exc:
        console.print("[red]未安装 keyring[/red]，请先：uv pip install 'bellwether[secure]'")
        raise typer.Exit(code=1) from exc

    value = typer.prompt("请输入 API key（输入不回显）", hide_input=True).strip()
    if not value:
        console.print("[red]输入为空，未保存。[/red]")
        raise typer.Exit(code=1)
    keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, value)
    console.print(
        f"[green]已存入系统钥匙串[/green]（服务名 {KEYRING_SERVICE}）。"
        "环境变量 ANTHROPIC_API_KEY 存在时优先于钥匙串。"
    )


@app.command()
def models(
    config_path: str | None = typer.Option(None, "--config", help="指定 config.toml 路径"),
) -> None:
    """列出当前 API 地址下可用的模型 id（便于挑选填入 config.toml 或 --model）。"""
    config = load_config(config_path)
    if not config.anthropic_api_key:
        console.print("[red]未检测到 ANTHROPIC_API_KEY[/red]，无法查询模型列表。")
        raise typer.Exit(code=1)

    import httpx  # anthropic 的依赖，一定已安装

    base = (config.anthropic_base_url or "https://api.anthropic.com").rstrip("/")
    try:
        resp = httpx.get(
            f"{base}/v1/models",
            headers={
                "x-api-key": config.anthropic_api_key,
                "anthropic-version": "2023-06-01",
            },
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPStatusError as exc:
        console.print(
            f"[red]查询失败[/red]（HTTP {exc.response.status_code}）：{exc.response.text[:300]}"
        )
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        console.print(f"[red]请求出错[/red]：{exc}")
        console.print("[dim]该中转可能未实现 /v1/models，可直接向服务商询问可用模型名。[/dim]")
        raise typer.Exit(code=1) from exc

    data = payload.get("data", []) if isinstance(payload, dict) else []
    if not data:
        console.print("返回的模型列表为空——该中转可能未实现 /v1/models，需向服务商确认可用模型名。")
        raise typer.Exit(code=0)

    table = Table(title=f"可用模型 @ {base}")
    table.add_column("模型 id", style="cyan")
    table.add_column("显示名 / 说明")
    for item in data:
        if isinstance(item, dict):
            table.add_row(str(item.get("id", "")), str(item.get("display_name", "")))
        else:
            table.add_row(str(item), "")
    console.print(table)
    console.print(
        "[dim]把需要的 id 填进 config.toml 的 [models].<role>，或运行时用 --model 覆盖。[/dim]"
    )


@app.command()
def snapshot(
    root: str | None = typer.Option(
        None, "--root", help="快照根目录（默认 ~/.bellwether/snapshots）"
    ),
    markets: str | None = typer.Option(None, "--markets", help="逗号分隔市场过滤，如 US,HK"),
    smoke: bool = typer.Option(False, "--smoke", help="冒烟模式：每市场只抓前 3 只"),
    delay: float = typer.Option(0.7, "--delay", help="标的间隔秒数（礼貌限流）"),
    golden: str | None = typer.Option(None, "--golden", help="自定义黄金集 toml 路径"),
) -> None:
    """A0 每日原始快照：黄金集行情/基本面/新闻落盘 + manifest（不调用 LLM，不需要 API key）。"""
    from .snapshot import exit_code_for, run_snapshot

    market_list = [m.strip() for m in markets.split(",")] if markets else None
    with console.status("正在快照黄金集 ……"):
        manifest = run_snapshot(
            root, markets=market_list, smoke=smoke, delay=delay, golden_path=golden
        )

    total, failed = len(manifest["entries"]), len(manifest["failures"])
    table = Table(title=f"Bellwether 快照 · {manifest['date']}{'（smoke）' if smoke else ''}")
    table.add_column("市场", style="cyan")
    table.add_column("成功", justify="right")
    table.add_column("失败", justify="right")
    per_market: dict[str, list[int]] = {}
    for _key, entry in manifest["entries"].items():
        m = entry["market"]
        ok_fail = per_market.setdefault(m, [0, 0])
        ok_fail[1 if entry["errors"] else 0] += 1
    for m, (ok, fail) in sorted(per_market.items()):
        table.add_row(m, str(ok), f"[red]{fail}[/red]" if fail else "0")
    console.print(table)
    if failed:
        console.print(f"[yellow]{failed}/{total} 个标的存在失败项，详见 manifest.failures[/yellow]")
    code = exit_code_for(manifest)
    if code:
        raise typer.Exit(code=code)


@app.command()
def portfolio(
    symbols: list[str] = typer.Argument(..., help="多只股票代码，如 AAPL MSFT 600519"),  # noqa: B008
    period: str = typer.Option("1y", "--period", help="回溯区间，如 6mo / 1y"),
    config_path: str | None = typer.Option(None, "--config", help="指定 config.toml 路径"),
) -> None:
    """多只股票的组合/风险分析（相关性/波动率/回撤/集中度，确定性指标，不经 LLM）。"""
    config = load_config(config_path)
    from .analysis.portfolio import PortfolioModule
    from .report import render_portfolio

    try:
        with console.status("正在计算组合指标 ……"):
            report = PortfolioModule().compute(symbols, period=period)
    except BellwetherError as exc:
        console.print(f"[red]组合分析失败[/red]（{type(exc).__name__}）：{redact(str(exc))}")
        raise typer.Exit(code=1) from exc
    render_portfolio(report, show_disclaimer=config.report.disclaimer)


if __name__ == "__main__":
    app()
