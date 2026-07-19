"""M2-B0 批 B：orchestrator 结构化报告端到端（取数→submit 拒绝→重交→渲染）。"""

import json
from datetime import UTC, datetime

import pandas as pd
import pytest

from bellwether.agent.llm import ResilientLLM
from bellwether.core.context import AnalysisContext, FrozenClock
from bellwether.models import FundamentalData, NewsItem, TradingRules

AS_OF = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _tool_use(name, tool_input, block_id="tu_1"):
    msg = _Block(stop_reason="tool_use")
    msg.content = [_Block(type="tool_use", name=name, input=tool_input, id=block_id)]
    return msg


class FakeClient:
    def __init__(self, script):
        self._script = list(script)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self._script.pop(0)
        return item(kwargs) if callable(item) else item


class FakeProvider:
    market = "CN"
    source = "akshare"

    def resolve_symbol(self, query, *, context):
        return query.strip().upper()

    def get_ohlcv(self, symbol, start, end, interval="1d", adjust="default", *, context):
        df = pd.DataFrame(
            {
                "open": [10.0, 11.0],
                "high": [11.5, 12.5],
                "low": [9.5, 10.5],
                "close": [11.0, 12.0],
                "volume": [100.0, 200.0],
            },
            index=pd.to_datetime(["2026-07-16", "2026-07-17"]),
        )
        df.attrs["captured_at"] = AS_OF.isoformat()
        return df

    def get_fundamentals(self, symbol, *, context):
        return FundamentalData(symbol=symbol, currency="CNY", fetched_at=AS_OF, source="akshare")

    def get_news(self, symbol, limit=20, *, context):
        return [NewsItem(title="标题", url=None, published_at=None, summary=None)]

    def trading_rules(self):
        return TradingRules(
            market="CN", timezone="Asia/Shanghai", has_price_limit=True, settlement="T+1"
        )


@pytest.fixture()
def orch(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("bellwether.core.trace.DEFAULT_TRACE_ROOT", tmp_path / "traces")
    monkeypatch.setattr("bellwether.agent.orchestrator.DEFAULT_CAPTURE_ROOT", tmp_path / "cap")
    monkeypatch.setattr(
        "bellwether.agent.orchestrator.ProviderRegistry.for_symbol",
        classmethod(lambda cls, s: FakeProvider()),
    )
    from bellwether.agent.orchestrator import Orchestrator
    from bellwether.config import AppConfig

    return Orchestrator(AppConfig())


def _ctx():
    return AnalysisContext(as_of=AS_OF, capture_policy="live", clock=FrozenClock(AS_OF))


def test_structured_flow_reject_then_accept(orch):
    def submit_bad(_kwargs):
        # 第一次提交带裸数字 → 组装器必须拒绝
        return _tool_use(
            "submit_report",
            {"sections": [{"title": "概览", "claims": ["涨到 45 元"]}]},
            block_id="tu_s1",
        )

    def submit_good(kwargs):
        # 第二次提交引用真实 eid：从上一轮 tool_result 里挖 last_close 的 eid
        history = json.dumps(kwargs["messages"], ensure_ascii=False, default=str)
        eid = json.loads(
            [m for m in kwargs["messages"] if m["role"] == "user"][1]["content"][0]["content"]
        )["last_close"]["eid"]
        assert "rejected" not in history or True
        return _tool_use(
            "submit_report",
            {
                "sections": [{"title": "概览", "claims": [f"收盘价为 [{eid}]"]}],
                "risks": [f"若 [{eid}] 水平失守需警惕"],
            },
            block_id="tu_s2",
        )

    orch.llm = ResilientLLM(
        FakeClient(
            [
                _tool_use("get_price_history", {"symbol": "600519"}, "tu_d1"),
                submit_bad,
                submit_good,
            ]
        )
    )
    text = orch.analyze("600519", context=_ctx())

    assert "[E" not in text and "12" in text  # 令牌已替换为真实数值
    assert "收盘价为" in text and "主要风险" in text

    trace = json.loads(orch.last_trace_path.read_text(encoding="utf-8"))
    assert trace["outcome"] == "ok"
    submit_calls = [t for t in trace["tool_calls"] if t["name"] == "submit_report"]
    assert len(submit_calls) == 2
    assert submit_calls[0]["output"].startswith("rejected")
    assert submit_calls[1]["output"] == "accepted"

    assert orch.last_report_path is not None and orch.last_report_path.exists()
    report = json.loads(orch.last_report_path.read_text(encoding="utf-8"))
    assert report["meta"]["symbol"] == "600519"
    assert report["meta"]["coverage"]["dims"]["ohlcv"]["status"] == "available"
    assert all(
        e["source"] is None or e["source"]["tool_call_id"] == "tu_d1"
        for e in report["evidence"].values()
    )


def test_unstructured_fallback_is_flagged(orch):
    end_turn = _Block(stop_reason="end_turn")
    end_turn.content = [_Block(type="text", text="纯文本研判")]
    end_turn2 = _Block(stop_reason="end_turn")
    end_turn2.content = [_Block(type="text", text="仍是纯文本")]
    client = FakeClient([end_turn, end_turn2])
    orch.llm = ResilientLLM(client)

    text = orch.analyze("600519", context=_ctx())
    assert "未经构造性核验管道" in text  # 回退必须明示，不得伪装成核验产物
    # 第二轮被强制 tool_choice=submit_report（mock 忽略但参数必须传了）
    assert client.calls[1].get("tool_choice") == {"type": "tool", "name": "submit_report"}
    trace = json.loads(orch.last_trace_path.read_text(encoding="utf-8"))
    assert trace["outcome"] == "unstructured"
    assert orch.last_report_path is None  # 未经管道不产 report.json
