"""C2a cassette 层（RFC-000 §9 / spec-001 §1 / spec-002 §1）：冻结输入的录制与重放。

按 `(provider_id, method, args)` 精确键值对提供数据——评测确定性的一次性冻结输入，
与 A0 快照（全窗日增观测流）职责分离（RFC-000 §9 DR14，不共享目录布局）。

目录形态：
    <root>/entries/<key>.json   —— 单条目，值 = canonical_json_bytes(payload)
    <root>/manifest.json        —— cassette_version + as_of + license_tag + 逐键 sha256 清单
    <root>/_COMPLETE            —— 原子完成标记（A0 snapshot.py 同惯例）

args 构造只经本文件的 `ohlcv_args`/`fundamentals_args`/`news_args`：录制
（`CassetteRecorder`）与重放（`CassetteProvider`）共用同一入口，是命中率的生命线——
任何一端擅自另起字段形态都会导致全量 miss。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from ..core.capture import canonical_json_bytes
from ..core.context import AnalysisContext
from ..core.exceptions import ConfigError, DataUnavailableError
from ..models import FundamentalData, NewsItem, TradingRules
from .base import MarketDataProvider, ProviderRegistry, detect_market


def provider_id_for_market(manifest: dict[str, Any], market: str) -> str:
    """从 cassette manifest 找某市场录制时的 provider_id（重放端命名一致的前提）。"""
    return next(
        (
            entry["provider_id"]
            for entry in manifest["entries"].values()
            if detect_market(entry["args"].get("symbol", "")) == market
        ),
        "recorded",
    )


_OHLCV_COLS = ["open", "high", "low", "close", "volume"]


def cassette_key(provider_id: str, method: str, args: dict[str, Any]) -> str:
    """`(provider_id, method, args)` → cassette 条目键（RFC-000 §8 规范化序列化后
    取 sha256 前 24 位）。"""
    preimage = {"provider_id": provider_id, "method": method, "args": args}
    return hashlib.sha256(canonical_json_bytes(preimage)).hexdigest()[:24]


def ohlcv_args(symbol: str, start: date, end: date) -> dict[str, Any]:
    """get_ohlcv 查询键的规范 args（录制/重放两端唯一构造入口）。"""
    return {"symbol": symbol, "start": str(start), "end": str(end)}


def fundamentals_args(symbol: str) -> dict[str, Any]:
    """get_fundamentals 查询键的规范 args。"""
    return {"symbol": symbol}


def news_args(symbol: str, limit: int) -> dict[str, Any]:
    """get_news 查询键的规范 args。"""
    return {"symbol": symbol, "limit": limit}


class CassetteRecorder:
    """录制端：provider 响应按键写入 `entries/<key>.json`；finalize 落 manifest + `_COMPLETE`。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._entries_dir = self.root / "entries"
        self._manifest_entries: dict[str, dict[str, Any]] = {}

    def record(
        self, provider_id: str, method: str, args: dict[str, Any], payload: dict[str, Any]
    ) -> str:
        """写一条 cassette 条目（内容寻址覆盖，幂等），返回其键。"""
        key = cassette_key(provider_id, method, args)
        body = canonical_json_bytes(payload)
        self._entries_dir.mkdir(parents=True, exist_ok=True)
        (self._entries_dir / f"{key}.json").write_bytes(body)
        self._manifest_entries[key] = {
            "provider_id": provider_id,
            "method": method,
            "args": args,
            "sha256": hashlib.sha256(body).hexdigest(),
        }
        return key

    def finalize(self, as_of: datetime, license_tag: str = "private-ok-backup-ok") -> Path:
        """写 `manifest.json` + `_COMPLETE` 原子完成标记（A0 snapshot.py 同惯例），
        返回 manifest 路径。"""
        manifest = {
            "cassette_version": 1,
            "as_of": as_of.isoformat(),
            "license_tag": license_tag,
            "entries": self._manifest_entries,
        }
        self.root.mkdir(parents=True, exist_ok=True)
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (self.root / "_COMPLETE").touch()
        return manifest_path


class CassetteProvider(MarketDataProvider):
    """重放端：查键命中直接重建返回值；未命中/未录完显式失败，绝不静默造数据。

    重放语义（spec-001 §1/§3）：返回的 DataFrame 不带 `attrs["captured_at"]`——
    `ToolRecorder` 按 `context.capture_policy="cassette"` 落 `pit_class="replay"`。
    """

    def __init__(self, root: str | Path, *, market: str, inner_source_name: str):
        self.root = Path(root)
        self.market = market
        self._inner_source_name = inner_source_name
        self.source = f"cassette:{inner_source_name}"
        if not (self.root / "_COMPLETE").exists():
            raise ConfigError(f"cassette 未完成录制（缺 _COMPLETE）：{self.root}")

    def _lookup(self, method: str, args: dict[str, Any]) -> dict[str, Any]:
        key = cassette_key(self._inner_source_name, method, args)
        path = self.root / "entries" / f"{key}.json"
        if not path.exists():
            raise DataUnavailableError(
                f"cassette 未命中：provider_id={self._inner_source_name!r} method={method!r} "
                f"args={args!r} key={key}"
            )
        return json.loads(path.read_bytes())

    def get_ohlcv(
        self,
        symbol: str,
        start: date,
        end: date,
        interval: str = "1d",
        adjust: str = "default",
        *,
        context: AnalysisContext,
    ) -> pd.DataFrame:
        if interval != "1d" or adjust != "default":
            raise DataUnavailableError(
                f"cassette 仅录制 interval='1d'/adjust='default' 视图，"
                f"收到 interval={interval!r} adjust={adjust!r}"
            )
        payload = self._lookup("get_ohlcv", ohlcv_args(symbol, start, end))
        df = pd.DataFrame(payload["records"])
        df["date"] = pd.to_datetime(df["date"])
        return df.set_index("date")[_OHLCV_COLS]

    def get_fundamentals(self, symbol: str, *, context: AnalysisContext) -> FundamentalData:
        payload = self._lookup("get_fundamentals", fundamentals_args(symbol))
        return FundamentalData.model_validate(payload)

    def get_news(self, symbol: str, limit: int = 20, *, context: AnalysisContext) -> list[NewsItem]:
        payload = self._lookup("get_news", news_args(symbol, limit))
        return [NewsItem.model_validate(item) for item in payload["items"]]

    def trading_rules(self) -> TradingRules:
        # 静态市场规则，非「数据」——不进 cassette，直接借真 provider 的常量定义。
        return ProviderRegistry.for_market(self.market).trading_rules()
