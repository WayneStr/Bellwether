"""Live 可寻址捕获层（spec-001 v1.1 §1 / ADR-0006 P4）+ RFC-000 §8 规范化序列化。

live 分析路径的每次 tool 响应都持久化为可寻址捕获：规范化字节 + sha256 +
capture_id（内容寻址，去重幂等）+ manifest 事件行。R7 溯源解析靠 resolve/verify
把「指针格式合法」升级为「指针解析到哈希吻合的真实字节」。

规范化序列化自持实现（不依赖 json.dumps 的浮点行为随库漂移），按 RFC-000 §8 M2 口径：
键排序、ensure_ascii=False 语义、浮点最短往返十进制（禁科学计数法）、
时间戳 UTC ISO-8601（+00:00 后缀）、无尾随空白。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


def _fmt_float(x: float) -> str:
    """最短往返十进制、禁科学计数（RFC-000 §8）。NaN/Inf 拒绝；-0.0 归一为 0.0。"""
    if x != x or x in (float("inf"), float("-inf")):
        raise ValueError("non-finite float is not canonicalizable")
    if x == 0.0:
        return "0.0"
    shortest = repr(x)
    if "e" in shortest or "E" in shortest:
        return format(Decimal(shortest), "f")
    return shortest


def _canonical(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _fmt_float(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetime is not canonicalizable")
        return json.dumps(value.astimezone(UTC).isoformat())
    if isinstance(value, date):
        return json.dumps(value.isoformat())
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical(v) for v in value) + "]"
    if isinstance(value, dict):
        items = sorted((str(k), v) for k, v in value.items())
        parts = (f"{json.dumps(k, ensure_ascii=False)}:{_canonical(v)}" for k, v in items)
        return "{" + ",".join(parts) + "}"
    raise TypeError(f"type {type(value).__name__} is not canonicalizable")


def canonical_json_bytes(value: Any) -> bytes:
    """RFC-000 §8 规范化 JSON 字节（哈希与落盘的唯一形态）。"""
    return _canonical(value).encode("utf-8")


class CaptureStore:
    """会话级捕获库：objects/<capture_id>.json（内容寻址字节）+ manifest.jsonl（事件行）。"""

    def __init__(self, root: Path):
        self.root = root
        self._objects = root / "objects"
        self._manifest = root / "manifest.jsonl"

    def persist(
        self,
        payload: Any,
        *,
        tool_call_id: str,
        provider_id: str,
        tool_name: str,
        canonical_request: dict[str, Any],
        captured_at: datetime,
        license_tag: str,
        upstream_source: str | None = None,
    ) -> tuple[str, str]:
        """落一次捕获，返回 (capture_id, response_sha256)。

        字节内容寻址（同响应去重、写入幂等）；事件逐条追加进 manifest
        （capture_id 是「哪份字节」，manifest 行是「哪次捕获事件」）。
        """
        body = canonical_json_bytes(payload)
        sha = hashlib.sha256(body).hexdigest()
        capture_id = sha[:16]
        self._objects.mkdir(parents=True, exist_ok=True)
        obj_path = self._objects / f"{capture_id}.json"
        if not obj_path.exists():
            obj_path.write_bytes(body)
        event = {
            "capture_id": capture_id,
            "response_sha256": sha,
            "tool_call_id": tool_call_id,
            "provider_id": provider_id,
            "upstream_source": upstream_source,
            "tool_name": tool_name,
            "canonical_request": canonical_request,
            "captured_at": captured_at,
            "license_tag": license_tag,
        }
        with self._manifest.open("a", encoding="utf-8") as f:
            f.write(_canonical(event) + "\n")
        return capture_id, sha

    def resolve(self, capture_id: str) -> bytes | None:
        """R7：capture_id → 规范化字节；不存在返回 None。"""
        path = self._objects / f"{capture_id}.json"
        if not path.exists():
            return None
        return path.read_bytes()

    def verify(self, capture_id: str, expected_sha256: str) -> bool:
        """R7 核验：捕获存在且全文哈希与 Evidence 声称的 response_sha256 吻合。"""
        body = self.resolve(capture_id)
        return body is not None and hashlib.sha256(body).hexdigest() == expected_sha256

    def events(self) -> list[dict[str, Any]]:
        """读取全部捕获事件（审计/R7 的 tool_call_id 归属核验）。"""
        if not self._manifest.exists():
            return []
        return [
            json.loads(line)
            for line in self._manifest.read_text(encoding="utf-8").splitlines()
            if line
        ]
