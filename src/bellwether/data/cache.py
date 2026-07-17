"""极简本地缓存：把取到的 DataFrame 缓存到 .cache/，带 TTL。

P0 只缓存行情（用 pandas pickle，无额外依赖）。config.data.cache_ttl_days
的接入留到 P1（届时由上层把 ttl 传进 provider）。
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from pathlib import Path

import pandas as pd

CACHE_DIR = Path(".cache")
DEFAULT_TTL_DAYS = 1


def _key_to_path(key: str) -> Path:
    digest = hashlib.sha1(key.encode()).hexdigest()[:16]
    return CACHE_DIR / f"{digest}.pkl"


def cached_dataframe(key: str, ttl_days: int, loader: Callable[[], pd.DataFrame]) -> pd.DataFrame:
    """命中且未过期则返回缓存，否则调用 loader 取数并写缓存。

    缓存读写失败一律降级为直接取数，绝不影响主流程。
    """
    path = _key_to_path(key)
    if path.exists():
        age_days = (time.time() - path.stat().st_mtime) / 86400
        if age_days <= ttl_days:
            try:
                return pd.read_pickle(path)
            except Exception:
                pass  # 缓存损坏 → 重新取

    df = loader()
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        df.to_pickle(path)
    except Exception:
        pass  # 写缓存失败不影响主流程
    return df
