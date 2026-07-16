"""A0 快照任务单测（假 provider，不打网）。"""

import json
from datetime import datetime, timezone

import pandas as pd
import pytest

from bellwether.models import FundamentalData, NewsItem
from bellwether.snapshot import exit_code_for, load_golden_set, run_snapshot


class _FakeProvider:
    source = "fake"

    def __init__(self, fail_symbol: str | None = None):
        self.fail_symbol = fail_symbol

    def resolve_symbol(self, q):
        return q.upper()

    def get_ohlcv(self, symbol, start, end, interval="1d", adjust="default"):
        if symbol == self.fail_symbol:
            raise ValueError("simulated outage")
        idx = pd.date_range("2026-01-01", periods=5, freq="D")
        return pd.DataFrame(
            {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100.0}, index=idx
        )

    def get_fundamentals(self, symbol):
        return FundamentalData(
            symbol=symbol, fetched_at=datetime.now(timezone.utc), source="fake"
        )

    def get_news(self, symbol, limit=20):
        return [NewsItem(title="t1"), NewsItem(title="t2")]


@pytest.fixture
def golden_file(tmp_path):
    p = tmp_path / "golden.toml"
    p.write_text(
        '[symbols]\nUS = ["AAA", "BBB"]\nCN = ["600000"]\n', encoding="utf-8"
    )
    return p


def _patch_provider(monkeypatch, provider):
    monkeypatch.setattr(
        "bellwether.snapshot.ProviderRegistry.for_market", classmethod(lambda cls, m: provider)
    )


def test_builtin_golden_set_shape():
    gs = load_golden_set()
    assert set(gs) == {"US", "CN", "HK"}
    for market, syms in gs.items():
        assert len(syms) == 30, market
        assert len(set(syms)) == 30, f"{market} 有重复标的"


def test_run_snapshot_writes_files_and_manifest(tmp_path, monkeypatch, golden_file):
    _patch_provider(monkeypatch, _FakeProvider())
    manifest = run_snapshot(
        tmp_path / "snaps", golden_path=golden_file, delay=0, date_str="2026-07-16"
    )
    day = tmp_path / "snaps" / "2026-07-16"
    assert (day / "US" / "AAA" / "ohlcv.csv").exists()
    assert (day / "US" / "AAA" / "ohlcv_raw.csv").exists()  # 事实层双抓（ADR-0003）
    assert (day / "US" / "AAA" / "fundamentals.json").exists()
    assert (day / "US" / "AAA" / "news.json").exists()
    assert (day / "manifest.json").exists()
    assert (tmp_path / "snaps" / "last_status.json").exists()

    entry = manifest["entries"]["US:AAA"]
    assert entry["files"]["ohlcv"]["rows"] == 5
    assert len(entry["files"]["ohlcv"]["sha256"]) == 64
    assert not manifest["failures"]
    assert exit_code_for(manifest) == 0

    status = json.loads((tmp_path / "snaps" / "last_status.json").read_text())
    assert status["ok"] is True and status["total"] == 3


def test_run_snapshot_records_failures_without_aborting(tmp_path, monkeypatch, golden_file):
    _patch_provider(monkeypatch, _FakeProvider(fail_symbol="AAA"))
    manifest = run_snapshot(tmp_path / "s", golden_path=golden_file, delay=0)
    assert "ohlcv" in manifest["failures"]["US:AAA"]
    # 失败标的的其他数据类与其他标的不受影响
    assert "fundamentals" in manifest["entries"]["US:AAA"]["files"]
    assert not manifest["entries"]["US:BBB"]["errors"]
    assert exit_code_for(manifest) == 2  # 部分失败


def test_market_filter_and_smoke(tmp_path, monkeypatch):
    _patch_provider(monkeypatch, _FakeProvider())
    big = tmp_path / "big.toml"
    big.write_text(
        '[symbols]\nUS = ["A1","A2","A3","A4","A5"]\nCN = ["C1"]\n', encoding="utf-8"
    )
    manifest = run_snapshot(
        tmp_path / "s", golden_path=big, markets=["US"], smoke=True, delay=0
    )
    keys = list(manifest["entries"])
    assert keys == ["US:A1", "US:A2", "US:A3"]  # 只有 US、且 smoke 截前 3

    # smoke 写独立 manifest，且不得触碰全量告警面 last_status.json
    day_dirs = list((tmp_path / "s").glob("*/manifest-smoke.json"))
    assert len(day_dirs) == 1
    assert not (tmp_path / "s" / "last_status.json").exists()


def test_exit_code_total_failure(tmp_path, monkeypatch, golden_file):
    class _Dead(_FakeProvider):
        def get_ohlcv(self, *a, **k):
            raise ValueError("down")

        def get_fundamentals(self, *a, **k):
            raise ValueError("down")

        def get_news(self, *a, **k):
            raise ValueError("down")

    _patch_provider(monkeypatch, _Dead())
    manifest = run_snapshot(tmp_path / "s", golden_path=golden_file, delay=0)
    assert exit_code_for(manifest) == 1
