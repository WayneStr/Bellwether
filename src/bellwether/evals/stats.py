"""C3 配对统计（RFC-003 §4.1/§4.2）：维度分数提取、逐例配对差、case 聚类 bootstrap。

全部纯函数、seed 显式：同输入同 seed 逐位同结果（门禁判定必须可复现可审计）。
判「配对退化」= mean(d) 的单侧 bootstrap 置信区间上界 < 0（显著为负）；
三层（PR/nightly/release）共用同一统计量，只变样本量与置信水平。
"""

from __future__ import annotations

import random
import statistics

from .models import CaseResult

# composite 的维度→分数映射见 case_score；skip/unverifiable 不计入（诚实缺席优于假 0 分）


def dimension_score(case: CaseResult, dimension: str) -> float | None:
    """单例某维度的 0-100 分数；无法评出分数（skip/unverifiable/缺维度）返回 None。

    映射（eval/gates.yaml 头注同步）：zero 容忍维 pass=100 / fail=0；
    completeness = score×100；reasoning = judge 均值；composite = 可用维度等权均值。
    """
    if case.error is not None:
        return 0.0  # schema 拒绝 = 整例确凿不合格，所有维度 0 分
    if dimension == "composite":
        parts = [
            s
            for name in ("factual", "completeness", "compliance", "reasoning")
            if (s := dimension_score(case, name)) is not None
        ]
        return statistics.fmean(parts) if parts else None
    dim = next((d for d in case.dimensions if d.name == dimension), None)
    if dim is None or dim.status in ("skip", "unverifiable"):
        return None
    if dimension == "completeness":
        return (dim.score if dim.score is not None else 0.0) * 100
    if dimension == "reasoning":
        return dim.score if dim.status == "pass" else 0.0
    return 100.0 if dim.status == "pass" else 0.0


def paired_diffs(
    candidate: list[CaseResult], baseline: list[CaseResult], dimension: str
) -> tuple[list[float], int]:
    """逐例配对差 dᵢ = candᵢ − baseᵢ（键 = symbol/market/tier）。

    返回 (diffs, unmatched)：unmatched = 无法配对或任一侧无分数而被剔除的例数
    （呈现层必须如实报告剔除量——静默丢样本会让门禁虚宽）。
    """

    def key(c: CaseResult) -> tuple:
        return (c.symbol, c.market, c.tier)

    base_by_key = {key(c): c for c in baseline}
    diffs: list[float] = []
    unmatched = 0
    for cand in candidate:
        base = base_by_key.get(key(cand))
        if base is None:
            unmatched += 1
            continue
        cand_score = dimension_score(cand, dimension)
        base_score = dimension_score(base, dimension)
        if cand_score is None or base_score is None:
            unmatched += 1
            continue
        diffs.append(cand_score - base_score)
    return diffs, unmatched


def null_distribution(runs: list[list[CaseResult]], dimension: str) -> dict:
    """同配置 k 次运行的两两 mean(d) null 分布（RFC-003 §4.1）。

    返回全部 pair means（以 0 为中心的抽样参照）与摘要统计；对照用法：候选的
    mean(d) 若落在 |pair_means| 最大值之外，即超出「无真实退化时的噪声带」。
    小样本（C(k,2)=10 对）下给出全列表比给分位数更诚实。
    """
    from itertools import combinations

    pair_means: list[float] = []
    dropped_pairs = 0
    for a, b in combinations(range(len(runs)), 2):
        diffs, _ = paired_diffs(runs[a], runs[b], dimension)
        if diffs:
            pair_means.append(round(statistics.fmean(diffs), 4))
        else:
            dropped_pairs += 1
    if not pair_means:
        return {"pairs": 0, "dropped_pairs": dropped_pairs, "pair_means": []}
    return {
        "pairs": len(pair_means),
        "dropped_pairs": dropped_pairs,
        "pair_means": sorted(pair_means),
        "mean": round(statistics.fmean(pair_means), 4),
        "stdev": round(statistics.stdev(pair_means), 4) if len(pair_means) > 1 else None,
        "abs_max": round(max(abs(m) for m in pair_means), 4),
    }


def bootstrap_upper_bound(
    diffs: list[float], *, confidence: float = 0.95, n_boot: int = 10_000, seed: int = 0
) -> tuple[float, float]:
    """mean(d) 与其单侧 bootstrap 置信区间上界（按 case 重采样，尊重聚类结构）。

    上界 < 0 ⇒ 配对退化显著（RFC-003 §4.2 判据）。seed 固定保证判定可复现。
    """
    if not diffs:
        raise ValueError("empty diffs")
    mean = statistics.fmean(diffs)
    if len(diffs) == 1:
        return mean, mean  # 单例无从重采样：上界=点估计（PR 层最小样本的诚实退化）
    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(statistics.fmean(rng.choices(diffs, k=n)) for _ in range(n_boot))
    upper_idx = min(n_boot - 1, int(confidence * n_boot))
    return mean, means[upper_idx]
