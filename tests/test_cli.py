"""CLI 命令单测（typer.testing.CliRunner）。全部 mock，不打网、不调真 LLM。

断言以 result.exit_code 为主；rich 输出带样式字符，文本断言宽松。
"""

import httpx
from typer.testing import CliRunner

from bellwether.cli import app
from bellwether.config import KEYRING_SERVICE, KEYRING_USERNAME
from bellwether.core.exceptions import BellwetherError, ModelNotFoundError

runner = CliRunner()


def _no_api_key(monkeypatch):
    """清空环境变量 + 关闭钥匙串兜底，模拟未设置 API key（避免本机真实钥匙串干扰）。"""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("bellwether.config._keyring_get_api_key", lambda: None)


def _make_orchestrator(*, verdict="研判结论", trace_path=None, error=None):
    """构造一个假 Orchestrator 类：analyze 返回固定 verdict，或抛出指定异常。"""

    class _FakeOrchestrator:
        def __init__(self, config):
            self.config = config
            self.last_trace_path = trace_path

        def analyze(self, symbol, *, context=None, deep=False, model_override=None, **overrides):
            if error is not None:
                raise error
            return verdict

    return _FakeOrchestrator


class _FakeHttpResponse:
    """httpx.get 返回值替身。"""

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


# ─────────────────────────── analyze ───────────────────────────
def test_analyze_no_api_key_exits_1(monkeypatch, tmp_path):
    _no_api_key(monkeypatch)
    result = runner.invoke(app, ["analyze", "AAPL", "--config", str(tmp_path / "nope.toml")])
    assert result.exit_code == 1


def test_analyze_success(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(
        "bellwether.agent.orchestrator.Orchestrator", _make_orchestrator(verdict="研判结论文本")
    )
    result = runner.invoke(app, ["analyze", "AAPL", "--config", str(tmp_path / "nope.toml")])
    assert result.exit_code == 0


def test_analyze_success_with_trace_path(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    trace_path = tmp_path / "trace.json"
    monkeypatch.setattr(
        "bellwether.agent.orchestrator.Orchestrator",
        _make_orchestrator(verdict="研判结论文本", trace_path=trace_path),
    )
    result = runner.invoke(app, ["analyze", "AAPL", "--config", str(tmp_path / "nope.toml")])
    assert result.exit_code == 0


def test_analyze_exports_markdown(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(
        "bellwether.agent.orchestrator.Orchestrator", _make_orchestrator(verdict="导出内容标记")
    )
    out_path = tmp_path / "out.md"
    result = runner.invoke(
        app,
        ["analyze", "AAPL", "--config", str(tmp_path / "nope.toml"), "-o", str(out_path)],
    )
    assert result.exit_code == 0
    assert out_path.exists()
    assert "导出内容标记" in out_path.read_text(encoding="utf-8")


def test_analyze_model_not_found_exits_1(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(
        "bellwether.agent.orchestrator.Orchestrator",
        _make_orchestrator(error=ModelNotFoundError("模型不存在")),
    )
    result = runner.invoke(app, ["analyze", "AAPL", "--config", str(tmp_path / "nope.toml")])
    assert result.exit_code == 1


# ─────────────────────────── config show ───────────────────────────
def test_config_show_key_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    result = runner.invoke(app, ["config", "show", "--config", str(tmp_path / "nope.toml")])
    assert result.exit_code == 0
    assert "环境变量" in result.output


def test_config_show_key_from_keyring(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("bellwether.config._keyring_get_api_key", lambda: "keyring-key")
    result = runner.invoke(app, ["config", "show", "--config", str(tmp_path / "nope.toml")])
    assert result.exit_code == 0
    assert "系统钥匙串" in result.output


def test_config_show_key_unset(monkeypatch, tmp_path):
    _no_api_key(monkeypatch)
    result = runner.invoke(app, ["config", "show", "--config", str(tmp_path / "nope.toml")])
    assert result.exit_code == 0
    assert "未设置" in result.output


# ─────────────────────────── config set-key ───────────────────────────
def test_config_set_key_success(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "keyring.set_password",
        lambda service, username, value: calls.append((service, username, value)),
    )
    result = runner.invoke(app, ["config", "set-key"], input="test-key\n")
    assert result.exit_code == 0
    assert calls == [(KEYRING_SERVICE, KEYRING_USERNAME, "test-key")]


def test_config_set_key_empty_input_exits_1():
    result = runner.invoke(app, ["config", "set-key"], input=" \n")
    assert result.exit_code == 1


# ─────────────────────────── models ───────────────────────────
def test_models_success(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    payload = {"data": [{"id": "m1", "display_name": "M1"}]}
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _FakeHttpResponse(payload))
    result = runner.invoke(app, ["models", "--config", str(tmp_path / "nope.toml")])
    assert result.exit_code == 0
    assert "m1" in result.output


def test_models_http_status_error_exits_1(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    def _boom(*a, **kw):
        request = httpx.Request("GET", "http://x")
        response = httpx.Response(500, request=request, json={"error": "boom"})
        raise httpx.HTTPStatusError("500 error", request=request, response=response)

    monkeypatch.setattr("httpx.get", _boom)
    result = runner.invoke(app, ["models", "--config", str(tmp_path / "nope.toml")])
    assert result.exit_code == 1


def test_models_empty_data_exits_0(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr("httpx.get", lambda *a, **kw: _FakeHttpResponse({"data": []}))
    result = runner.invoke(app, ["models", "--config", str(tmp_path / "nope.toml")])
    assert result.exit_code == 0


# ─────────────────────────── portfolio ───────────────────────────
def test_portfolio_success(monkeypatch, tmp_path):
    calls = []

    class _FakePortfolioModule:
        def compute(self, symbols, period="1y", context=None):
            return "FAKE_REPORT"

    def fake_render(report, *, show_disclaimer=True):
        calls.append((report, show_disclaimer))

    monkeypatch.setattr("bellwether.analysis.portfolio.PortfolioModule", _FakePortfolioModule)
    monkeypatch.setattr("bellwether.report.render_portfolio", fake_render)

    result = runner.invoke(
        app, ["portfolio", "AAPL", "MSFT", "--config", str(tmp_path / "nope.toml")]
    )
    assert result.exit_code == 0
    assert calls == [("FAKE_REPORT", True)]


def test_portfolio_bellwether_error_exits_1(monkeypatch, tmp_path):
    class _FailingPortfolioModule:
        def compute(self, symbols, period="1y", context=None):
            raise BellwetherError("组合分析失败")

    monkeypatch.setattr("bellwether.analysis.portfolio.PortfolioModule", _FailingPortfolioModule)
    result = runner.invoke(
        app, ["portfolio", "AAPL", "MSFT", "--config", str(tmp_path / "nope.toml")]
    )
    assert result.exit_code == 1


def test_portfolio_value_error_exits_1(monkeypatch, tmp_path):
    # PortfolioModule 的输入校验抛裸 ValueError（标的太少等）→ 同样友好失败而非 traceback
    class _FailingPortfolioModule:
        def compute(self, symbols, period="1y", context=None):
            raise ValueError("组合分析至少需要 2 只标的")

    monkeypatch.setattr("bellwether.analysis.portfolio.PortfolioModule", _FailingPortfolioModule)
    result = runner.invoke(app, ["portfolio", "AAPL", "--config", str(tmp_path / "nope.toml")])
    assert result.exit_code == 1


# ─────────────────────────── snapshot ───────────────────────────
def test_snapshot_all_success_exits_0(monkeypatch, tmp_path):
    manifest = {
        "date": "2026-07-18",
        "entries": {
            "US:AAA": {"market": "US", "errors": []},
            "US:BBB": {"market": "US", "errors": []},
        },
        "failures": {},
    }
    monkeypatch.setattr("bellwether.snapshot.run_snapshot", lambda *a, **kw: manifest)
    result = runner.invoke(app, ["snapshot", "--root", str(tmp_path)])
    assert result.exit_code == 0


def test_snapshot_partial_failure_exits_2(monkeypatch, tmp_path):
    manifest = {
        "date": "2026-07-18",
        "entries": {
            "US:AAA": {"market": "US", "errors": []},
            "HK:00700": {"market": "HK", "errors": ["ohlcv 拉取失败"]},
        },
        "failures": {"HK:00700": ["ohlcv 拉取失败"]},
    }
    monkeypatch.setattr("bellwether.snapshot.run_snapshot", lambda *a, **kw: manifest)
    result = runner.invoke(app, ["snapshot", "--root", str(tmp_path), "--markets", "US,HK"])
    assert result.exit_code == 2
