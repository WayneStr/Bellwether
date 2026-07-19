"""get_news tool 单测（假 provider，不打网）。"""

import json
from datetime import UTC, datetime

from bellwether.agent.tools import execute_tool
from bellwether.models import NewsItem


class _FakeProvider:
    source = "fake"

    def resolve_symbol(self, query, context=None):
        return query.upper()

    def get_news(self, symbol, limit=10, context=None):
        items = [
            NewsItem(
                title="财报超预期",
                url="http://x",
                published_at=datetime(2026, 7, 1, tzinfo=UTC),
                summary="Q2 营收增长",
            ),
            NewsItem(title="监管调查", url=None, published_at=None, summary=None),
        ]
        return items[:limit]


def test_get_news_tool(ctx):
    out = execute_tool("get_news", {"symbol": "aapl", "limit": 5}, _FakeProvider(), context=ctx)
    data = json.loads(out)
    assert data["symbol"] == "AAPL"
    assert data["count"] == 2
    assert data["news"][0]["title"] == "财报超预期"
    assert data["news"][1]["published_at"] is None  # 缺失时间容错不崩


def test_get_news_respects_limit(ctx):
    out = execute_tool("get_news", {"symbol": "AAPL", "limit": 1}, _FakeProvider(), context=ctx)
    assert json.loads(out)["count"] == 1
