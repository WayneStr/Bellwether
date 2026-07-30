"""StructuredReport → Markdown 渲染器（spec-001 v1.1 §3）。

唯一的 token 替换渲染入口：报告中的每个数字都且只能来自一次 [E12] 替换
（数值 + 单位/币种，格式规则冻结如下），或 meta 结构化豁免字段。渲染前对
「替换前全文」执行 R5 运行时不变量检查——模板文本必须零裸数字，因此替换后
输出的数字必然全部可反查到某次替换。

冻结数字格式（回放确定性边界，ADR-0006 P6）：
- round(value, 2) 后十进制定点、去尾零（1710.0→"1710"、47.86→"47.86"、3.636→"3.64"）
- 绝对值 ≥ 10000 加千分位逗号
- unit 直接后缀（"47.86%"）；currency 空格后缀（"1234.56 CNY"）
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal

from .models import Evidence, StructuredReport
from .verify import scan_naked_numbers

_TOKEN = re.compile(r"\[E([1-9][0-9]*)\]")


def format_value(evidence: Evidence) -> str:
    """冻结的数值/文本格式化（rich 终端与 markdown 共享的唯一实现）。"""
    if isinstance(evidence.value, str):
        return f"「{evidence.value}」"
    quantized = Decimal(repr(evidence.value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    pattern = f"{quantized:,.2f}" if abs(quantized) >= 10000 else f"{quantized:.2f}"
    text = pattern.rstrip("0").rstrip(".")
    if evidence.unit:
        text += evidence.unit
    if evidence.currency:
        text += f" {evidence.currency}"
    if evidence.confidence == "stale":
        text += "⏳"  # stale 证据的行内标记（横幅在报告头部）
    return text


def render_report(report: StructuredReport) -> str:
    """渲染 Markdown 正文。R5 运行时不变量：替换前全文必须零裸数字。"""
    lines: list[str] = []

    stale = any(e.confidence == "stale" for e in report.evidence.values())
    if stale:
        lines.append("> ⚠️ **数据时效提示**：部分证据超过新鲜度阈值（行内以 ⏳ 标记）。")
        lines.append("")
    missing_dims = [
        name for name, d in report.meta.coverage.dims.items() if d.status != "available"
    ]
    if missing_dims:
        lines.append(f"> 数据覆盖：以下维度缺失或未接入——{'、'.join(missing_dims)}。")
        lines.append("")

    for section in report.sections:
        lines.append(f"## {section.title}")
        lines.extend(f"- {claim.text}" for claim in section.claims)
        lines.append("")

    if report.scenarios:
        lines.append("## 情景分析")
        names = {"bull": "乐观", "base": "中性", "bear": "悲观"}
        lines.extend(f"- **{names[s.name]}（{s.name}）**：{s.narrative}" for s in report.scenarios)
        lines.append("")

    if report.risks:
        lines.append("## 主要风险")
        lines.extend(f"- {claim.text}" for claim in report.risks)
        lines.append("")

    if report.meta.dropped_claims:
        lines.append(
            "> 注：部分陈述未通过构造性核验已被剔除（数量见 report.json 的 "
            "dropped_claims），详情见 provenance trace。"
        )
        lines.append("")

    template = "\n".join(lines).rstrip()

    # R5 运行时不变量：替换前的全文（标题/叙述/横幅）零裸数字——组装器已拦，
    # 此处违反说明管道外代码构造了报告，属实现缺陷，快败。
    if scan_naked_numbers(template):
        raise RuntimeError("render invariant violated: naked number in pre-substitution text")

    # 令牌替换：相邻令牌（模板里中间零字符，如某些模型把 [E1][E2][E3] 甩句尾）之间补一个
    # 空格，避免数值连串（40.3245.872.68）。有空格/标点分隔的正常情形不受影响。
    out: list[str] = []
    cursor = 0
    prev_token_end = -1
    for match in _TOKEN.finditer(template):
        out.append(template[cursor : match.start()])
        if match.start() == prev_token_end:  # 与上一令牌紧邻，无分隔字符
            out.append(" ")
        out.append(format_value(report.evidence[f"E{match.group(1)}"]))
        cursor = prev_token_end = match.end()
    out.append(template[cursor:])
    return "".join(out)
