"""A0 每日原始快照任务（ROADMAP §4 WS-A · A0）。

把黄金集标的的行情/基本面/新闻按日落盘为「原始层」快照 + 版本化 manifest：
- 这是评测黄金集（C2）与校准回测（C4）的时间物理资产——新闻等免费源无历史存档，
  只能从今天起逐日积累，晚一天少一天。
- 存放于项目外（默认 ~/.bellwether/snapshots/），**绝不进 git**（数据许可，ADR-0002）。
- 单标的/单数据类失败只记入 manifest.failures，不中断全局（live smoke 的告警面）。
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import random
import secrets
import time
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .data.base import ProviderRegistry

# v3: run-id 不可变目录（{root}/{date}/run-<HHMMSS>-<4位hex>/，同日多次运行不覆盖，
#     写完后落 _COMPLETE 原子完成标记）+ manifest/entry 元数据补齐
#     （run_id/provider_versions/license_tag/price_basis/actions_captured）
SCHEMA_VERSION = 3
DEFAULT_ROOT = Path.home() / ".bellwether" / "snapshots"
LOOKBACK_DAYS = 400
SMOKE_PER_MARKET = 3
LICENSE_TAG = "private-do-not-redistribute (pending E3 audit)"


def load_golden_set(path: str | Path | None = None) -> dict[str, list[str]]:
    """读取黄金集清单：{market: [symbols]}。默认用包内 golden_set.toml。"""
    p = Path(path) if path else Path(__file__).parent / "golden_set.toml"
    with open(p, "rb") as f:
        raw = tomllib.load(f)
    return {m.upper(): list(syms) for m, syms in raw["symbols"].items()}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_meta(path: Path, run_dir: Path, **extra) -> dict:
    return {
        "path": str(path.relative_to(run_dir)),
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
        **extra,
    }


def _price_basis(market: str) -> dict[str, str]:
    """按市场返回视图口径（ohlcv）与事实口径（ohlcv_raw）标注。"""
    if market == "US":
        return {"ohlcv": "split_and_dividend_adjusted", "ohlcv_raw": "split_adjusted_plus_action_columns"}
    return {"ohlcv": "qfq", "ohlcv_raw": "unadjusted"}


def _provider_versions() -> dict[str, str | None]:
    """已安装的第三方数据源包版本；包不存在则 null，不因此报错。"""
    versions: dict[str, str | None] = {}
    for pkg in ("yfinance", "akshare"):
        try:
            versions[pkg] = importlib.metadata.version(pkg)
        except importlib.metadata.PackageNotFoundError:
            versions[pkg] = None
    return versions


def snapshot_symbol(
    symbol: str,
    market: str,
    run_dir: Path,
    *,
    lookback_days: int = LOOKBACK_DAYS,
    intra_delay: float = 0.0,
) -> dict:
    """抓取单标的三类数据落盘。返回 manifest 条目；单项失败记 errors 不抛出。

    intra_delay：同一标的相邻两次抓取之间的间歇。东财 K 线端点对
    「view+raw 背靠背两连击 × 快速轮标的」的突发模式会 RemoteDisconnected
    （2026-07-17 首次全量实测），标的间 delay 之外还需要标的内间歇。
    """
    entry: dict = {
        "market": market,
        "files": {},
        "errors": {},
        "price_basis": _price_basis(market),
        "actions_captured": market == "US",
    }
    if market != "US":
        entry["actions_note"] = "corporate actions backfillable from exchange announcements; deferred to M3"
    out_dir = run_dir / market / symbol
    out_dir.mkdir(parents=True, exist_ok=True)

    provider = ProviderRegistry.for_market(market)
    sym = provider.resolve_symbol(symbol)
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=lookback_days)

    try:
        df = provider.get_ohlcv(sym, start, end)
        fp = out_dir / "ohlcv.csv"
        df.to_csv(fp)
        entry["files"]["ohlcv"] = _file_meta(fp, run_dir, rows=len(df), adjust="default")
    except Exception as exc:
        entry["errors"]["ohlcv"] = str(exc)

    if intra_delay > 0:
        time.sleep(intra_delay)
    try:
        # 事实层：不复权原始价（复权视图会因未来分红/送转全序列重写，只有 raw 不可重写）
        df_raw = provider.get_ohlcv(sym, start, end, adjust="raw")
        fp = out_dir / "ohlcv_raw.csv"
        df_raw.to_csv(fp)
        entry["files"]["ohlcv_raw"] = _file_meta(fp, run_dir, rows=len(df_raw), adjust="raw")
    except Exception as exc:
        entry["errors"]["ohlcv_raw"] = str(exc)

    try:
        fund = provider.get_fundamentals(sym)
        fp = out_dir / "fundamentals.json"
        fp.write_text(fund.model_dump_json(indent=2), encoding="utf-8")
        entry["files"]["fundamentals"] = _file_meta(fp, run_dir)
    except Exception as exc:
        entry["errors"]["fundamentals"] = str(exc)

    try:
        news = provider.get_news(sym, 20)
        fp = out_dir / "news.json"
        fp.write_text(
            json.dumps([n.model_dump(mode="json") for n in news], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        entry["files"]["news"] = _file_meta(fp, run_dir, count=len(news))
    except Exception as exc:
        entry["errors"]["news"] = str(exc)

    return entry


def run_snapshot(
    root: str | Path | None = None,
    *,
    markets: list[str] | None = None,
    smoke: bool = False,
    delay: float = 0.7,
    golden_path: str | Path | None = None,
    date_str: str | None = None,
) -> dict:
    """执行一次快照，返回 manifest（同时写盘 manifest.json 与根级 last_status.json）。"""
    root_dir = Path(root) if root else DEFAULT_ROOT
    universe = load_golden_set(golden_path)
    if markets:
        wanted = {m.upper() for m in markets}
        universe = {m: syms for m, syms in universe.items() if m in wanted}
    if smoke:
        universe = {m: syms[:SMOKE_PER_MARKET] for m, syms in universe.items()}

    day = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    run_id = f"run-{datetime.now(timezone.utc).strftime('%H%M%S')}-{secrets.token_hex(2)}"
    run_dir = root_dir / day / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict = {
        "schema_version": SCHEMA_VERSION,
        "date": day,
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "smoke": smoke,
        "provider_versions": _provider_versions(),
        "license_tag": LICENSE_TAG,
        "entries": {},
        "failures": {},
    }

    for market, syms in universe.items():
        for symbol in syms:
            entry = snapshot_symbol(symbol, market, run_dir, intra_delay=min(delay, 0.6))
            key = f"{market}:{symbol}"
            manifest["entries"][key] = entry
            if entry["errors"]:
                manifest["failures"][key] = entry["errors"]
            if delay > 0:
                time.sleep(delay + random.uniform(0, 0.3))  # 礼貌限流，防免费源封禁

    # smoke 运行写独立文件、不碰 last_status —— 手工冒烟不得污染每日全量任务的告警面
    manifest_name = "manifest-smoke.json" if smoke else "manifest.json"
    (run_dir / manifest_name).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_dir / "_COMPLETE").touch()  # 原子完成标记：全部文件+manifest 写完才落此空文件，读取者只认含它的 run
    if not smoke:
        total, failed = len(manifest["entries"]), len(manifest["failures"])
        (root_dir / "last_status.json").write_text(  # 告警面：机器可读的最近一次全量状态
            json.dumps(
                {
                    "date": day,
                    "run_id": run_id,
                    "run_path": str(run_dir.relative_to(root_dir)),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "total": total,
                    "failed": failed,
                    "ok": failed == 0,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return manifest


def exit_code_for(manifest: dict) -> int:
    """0=全部成功；2=部分失败（降级）；1=全军覆没。"""
    total, failed = len(manifest["entries"]), len(manifest["failures"])
    if total == 0 or failed == total:
        return 1
    return 2 if failed else 0
