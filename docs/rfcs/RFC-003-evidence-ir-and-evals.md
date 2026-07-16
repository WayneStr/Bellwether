# RFC-003: Claim/Evidence IR 与评测体系

| 字段 | 值 |
|------|-----|
| 状态 | **Draft**（M0 立项初稿） |
| 日期 | 2026-07-16 |
| 关联任务 | B0（IR + report schema + provenance）、C1（评测运行器）、C2a（冻结黄金集）、C3（噪声感知门禁） |
| 里程碑 | M1 冻结 IR spec → M2 落地 |
| 评审依据 | `docs/reviews/` 两份独立评审（deep-reasoner F4/F6/F7/F16；Codex #1/#4/#5/#6） |

## 0. 背景与范围

「数字零臆造」从事后文本核查升级为**构造性保证**：报告中的数字只能由 Claim/Evidence IR 渲染，LLM 只能引用证据 ID。评测（C1）校验 IR 而非自由文本；门禁（C3）先实测噪声再设阈值。本 RFC 回答 ROADMAP §3 的五道必答题。

**不覆盖**：C2b 多时点集与双时间存储（RFC-002）、C4 校准与 C5 公开基准细则（仅对齐 baseline-of-record 接口）、B2 受控实验设计（RFC-001）。

C1 判分四维（后文门禁均以此为准）：**事实性**（IR 溯源，程序化硬判）、**完整性**（分报告类型清单）、**推理质量**（LLM 评审 + rubric）、**合规**（M2 用规则层，E1 完整版后升级）。

## 1. 必答 1：Claim/Evidence IR 字段定稿

### 1.1 设计原则与受控范围（诚实声明）

- **证据 = 原子事实单元**：每个进入报告的数字或文本类事实（新闻标题、文件引文）都必须先成为一条 Evidence，携带完整溯源。
- **构造性保证的边界**：机械保证「报告中每个数字真实存在且可溯源」；**不**保证「引用语义正确」（LLM 把营收的 eid 用在利润叙述里仍可能发生）。语义正确性由 C1 推理质量维度 + B2 播种错误实验度量。KPI「受控 claim 范围内 100%」即指此边界。
- LLM 全程只见 eid 与只读事实，任何算术由代码完成（§1.3）。

### 1.2 Pydantic schema 草案（M1 冻结前可改，字段集为定稿候选）

```python
from datetime import date, datetime
from typing import Literal
from pydantic import BaseModel, Field

Confidence = Literal["reported", "derived", "estimated", "stale", "missing"]

class SourceRef(BaseModel):
    source_id: str                       # provider 标识："yfinance"/"akshare"/cassette id
    tool_name: str                       # 产生本证据的 tool，如 "get_fundamentals"
    tool_call_id: str                    # 关联 trace 中的具体调用（E4 provenance 键）
    url: str | None = None               # 新闻/文件类原始链接
    snapshot_sha256: str | None = None   # 对应 cassette 条目 / A0 快照哈希

class Period(BaseModel):
    kind: Literal["instant", "range", "fiscal"]
    start: date | None = None
    end: date | None = None
    label: str | None = None             # "FY2025" / "TTM" / "6mo"

class Derivation(BaseModel):
    op: str                              # 白名单运算名："yoy_pct"/"ratio"/"cagr"/...
    inputs: list[str]                    # 只收 eid，不收字面量（防洗白，见 §1.3）
    params: dict[str, float] = Field(default_factory=dict)  # 暂仅确定性模块可填（D2）
    formula: str                         # 人类可读："(E1-E2)/E2*100"

class Evidence(BaseModel):
    eid: str                             # "E7"：单次分析内递增分配，从不复用
    kind: Literal["metric", "series_stat", "news", "doc_quote"]
    value: float | str                   # 数值；news/doc_quote 存原文文本
    unit: str | None = None              # "%"/"倍"/"股"/"元/股"；文本类 None
    currency: str | None = None          # ISO 4217；纯比率/文本类 None
    period: Period | None = None
    as_of: datetime                      # 获知时间（对齐 RFC-002 双时间语义）
    source: SourceRef
    derivation: Derivation | None = None # None = 源原始值
    confidence: Confidence
    fingerprint: str | None = None       # 内容寻址哈希，跨运行 paired 对齐用（可选）
```

`EvidenceStore` 为单次分析的运行期容器：`register(...) -> eid`、`get(eid)`、`closure(eids)`（含 derivation 传递闭包）。置信状态渲染约定：`estimated` 必须连带假设呈现；`stale`（超新鲜度阈值）触发报告横幅；`missing` 触发 A9 维度静默。

### 1.3 派生值：一律代码预算，两条供给路径

1. **预算集（主）**：确定性分析模块在 tool 执行时即计算常用派生值并注册——区间收益、同比/环比、估值比率、DCF 敏感性格点。现有 `tools._summarize_ohlcv` 的 `period_return_pct` 即此类，B0 只是补上 Derivation 注册。
2. **`derive_metric` tool（辅）**：LLM 需要预算集之外的运算时，调用 `derive_metric(op, input_eids)`；op 限白名单纯函数（add/sub/mul/div/pct_change/ratio/cagr），代码执行、注册新 Evidence（confidence=derived）并返回 eid。**schema 上 inputs 只接受 eid、不接受数值字面量**——LLM 无法借该 tool 把臆造数「洗」成合法证据。带数值参数的假设类运算（DCF 情景改参）M2 暂不开放，随 B3 估值引擎再定 [OPEN O8]。

### 1.4 证据 ID 引用格式与防绕过

**引用格式**：LLM 产出的叙述文本中，数字位置只允许出现 `[E7]` 式令牌。渲染层将令牌替换为「格式化值 + 单位/币种」（中英本地化随 B8），并自动生成证据附表。渲染替换是最终报告数字的**唯一**来源。

**防绕过 = 核查器 `verify_constructive` 的五条机械规则**（对 LLM 原始输出、渲染前执行）：

| # | 规则 | 拦截的绕过方式 |
|---|------|----------------|
| R1 | 裸数字检测：正则检出阿拉伯数字与中文数字（万/亿），除白名单外一律违规。白名单：eid 令牌内部、列表序号、指标名内嵌数字（MA20/RSI14/10-K）、纪年与季度标签（2026 年/FY2025/Q1）[OPEN O1] | 直接在文本里写数 |
| R2 | eid 存在性：所有令牌必须 ∈ 本次 EvidenceStore | 幻造 `[E99]` |
| R3 | 双写一致：`Claim.evidence_ids` 与 text 中令牌抽取结果一致 | 结构与文本脱节 |
| R4 | 引用闭包：`report.evidence` 恰为引用闭包（含 derivation inputs 传递闭包） | 报告携带无来源证据 |
| R5 | 渲染自证：渲染输出中每个数值可反向映射到某次令牌替换（渲染器单测承担） | 渲染层自己引入数字 |

**失败处理**：违规 claim → 携带违规原因定点重写，重试 ≤2 次 → 仍违规则**丢弃该 claim** 并计入 `meta.dropped_claims`（宁缺毋滥，与 A9 同哲学）。误杀（白名单过严）表现为丢弃率升高，作为运营指标跟踪。

**KPI 口径**：由于违规 claim 被强制丢弃，最终报告的构造性溯源率恒为 100%——门禁真正断言的是**管道不变量**：CI 中的守门员测试用播种违规样本（裸数字/假 eid/闭包缺失）验证 verifier 在位且全拦截；任何绕开 verifier 的代码路径即门禁失败。健康度看丢弃率/重试率趋势。

## 2. 必答 2：machine-readable report schema

### 2.1 schema 草案

```python
class Claim(BaseModel):
    claim_id: str
    kind: Literal["fact", "interpretation", "scenario", "risk"]
    text: str                            # 叙述文本；数字位置只允许 [E*] 令牌
    evidence_ids: list[str]              # 与 text 令牌双写，供 R3 比对

class Section(BaseModel):
    section_id: str                      # "overview"/"technical"/"valuation"/"events"
    title: str
    claims: list[Claim]

class Scenario(BaseModel):
    name: Literal["bull", "base", "bear"]
    assumption_eids: list[str]           # 假设一律为 estimated 证据
    narrative: str                       # 同样只允许 [E*] 令牌

class CoverageEntry(BaseModel):
    module: str                          # A9 市场×模块覆盖矩阵条目
    status: Literal["ok", "partial", "missing"]

class ReportMeta(BaseModel):
    schema_version: str = "0.1"
    symbol: str
    market: Literal["US", "CN", "HK"]
    tier: Literal["quick", "deep"]
    generated_at: datetime
    model_versions: dict[str, str]       # 角色 → 模型 id（B9 重定基线的键）
    prompt_version: str
    coverage: list[CoverageEntry]
    dropped_claims: int = 0
    cost_usd: float | None = None

class StructuredReport(BaseModel):
    meta: ReportMeta
    sections: list[Section]
    scenarios: list[Scenario] = Field(default_factory=list)
    risks: list[Claim] = Field(default_factory=list)
    evidence: dict[str, Evidence]        # 引用闭包（eid → Evidence）
    provenance_ref: str                  # provenance manifest 路径+哈希（E4）
```

### 2.2 与渲染/导出的关系

`StructuredReport`（落盘为 `report.json`）是**唯一事实源**；rich 终端与 markdown 导出都是它的无状态视图函数：终端遍历 sections 渲染 Panel/Table 并自动附证据表，markdown 用同一令牌替换器输出。C1 只读 `report.json`，永不做文本数字抽取。回放（E4 trace playback）= 重放录制的 LLM 输出重建同一 `report.json` → 渲染结果必然一致。

### 2.3 现有代码迁移面

- `models.AnalysisResult`（自由文本 verdict）→ 由 `StructuredReport` 取代，quick 路径先行；`PortfolioReport` 已结构化，最后并入。
- `agent/tools.execute_tool` 返回 JSON → 每个数值旁**内联注入 eid**（如 `{"pe": {"v": 32.1, "eid": "E7"}}`），token 开销由 M2 已启用的 prompt caching 吸收（D3）。
- `report.render_analysis / export_markdown` → `render_report(StructuredReport)` 及导出视图。

## 3. 必答 3：分层确定性定义

| 层 | 对象 | 确定性 | 呈现/用法 |
|----|------|--------|-----------|
| A 输入 | cassette 重放的 provider 输出 | 字节级确定（哈希校验） | 评测前提，不出分 |
| B artifact 评分 | 对**某份已生成报告**跑程序化核查（R1–R5、完整性清单、合规规则、schema 校验） | 完全确定，重复评分逐位一致 | 零容忍与地板门禁的判据 |
| C 端到端 | 生成 + 程序化评分 | 生成本身随机（温度 0 也不保证 API 级确定） | 分数是分布；零容忍维度 = 每次每例都必须过 |
| D LLM 评审 | rubric 打分 | 双重随机（生成×评审） | n 次均值 ± 95% CI（t 区间） |

关键推论：**零容忍不与随机性冲突**——事实性/合规是 B 层 per-artifact 硬性质，任何一次运行任何一例违规即红，无需统计。统计只用于 C/D 层的连续分数。

**n 的取值与成本权衡**：方差预期主要来自生成而非评审（待 §4 噪声测定验证），预算优先给 n_gen；judge 用 haiku 级 + rubric + caching，n_judge=3 足以把评审方差压为次要项。建议：PR n_gen=1/n_judge=1；nightly 1/3；release **5/3**（每份报告 3 次评审取均值，再跨 5 次生成取均值±CI）。单次 nightly 只画趋势，不下结论。

## 4. 必答 4：噪声带测定与噪声感知门禁

### 4.1 噪声带测定

- **方法**：冻结 main 代码 + cassette + 钉死 judge 版本，在 nightly 半量集（90 例）上跑 **k=5** 次完整端到端评测。对每个 C/D 层维度 m 与综合分：取 k 个集合级分数，样本标准差 s_m，**噪声带半宽 W_m = t₀.₉₇₅,ₖ₋₁ · s_m**（k=5 时系数 2.78）。同时保留 5×90 的 per-case 分数矩阵（供 paired 标定）与极差作稳健性参照。
- **k=5 的理由**：k=3 时 t 系数 4.30、带宽过松（假阴性多）；k=10 成本翻倍、收益边际。实测 s 不稳定时增跑。
- **重测触发**（与 B9 联动）：模型版本 / prompt 主版本 / judge 或 rubric / cassette 版本任一变更 → 噪声带与基线强制重测。

### 4.2 paired 同题对比检验

candidate 与 **baseline-of-record**（钉死模型版本+prompt+cassette+预算，C5 同源）同题、同 cassette、同 judge。逐例差值 dᵢ = candᵢ − baseᵢ：

- **PR 层（20 例，检验功效不足）**：仅用主条件 `mean(d) < −W_m^PR` 阻断（W 按 20 例子集单独标定，带更宽）。
- **nightly / release**：`mean(d) < −W_m` **且** Wilcoxon 符号秩单侧检验 p<0.05 → 阻断（双条件 AND：噪声带挡随机波动，检验挡小样本侥幸；rubric 为 1–5 离散分，Wilcoxon 比 t 检验稳健）。
- flaky 率月度跟踪 = 重跑翻绿数/总阻断数，收敛目标 <10% [OPEN]（C3 验收项）。

### 4.3 分维度地板的落地形式

声明式配置 `eval/gates.yaml`，门禁引擎按此执行，任何改动走 PR 评审：

```yaml
dimensions:
  factual:      {gate: zero_tolerance}                  # B 层硬判，任何一例违规即红
  compliance:   {gate: zero_tolerance}
  completeness: {gate: absolute_min, floor: 0.95}       # [OPEN O5]
  reasoning:    {gate: noise_band, waiver: true}        # 豁免=PR 标签+签字理由，月报汇总
composite:      {gate: noise_band, waiver: false}
```

zero_tolerance 维度无豁免通道；waiver 仅对推理质量开放且留痕（防棘轮僵死，同时防豁免滥用）。

## 5. 必答 5：评测分层、选样与成本预算

### 5.1 黄金集与分层抽样

黄金集 = 30 标的 × 3 市场 × 2 档 = **180 用例**（决策 9）。分层维度 = 市场(US/CN/HK) × 档位(quick/deep) = 6 层。层内选样标准：市值大/中/小分布、行业分散、≥1 个已知数据缺陷标的（如 HK 基本面缺失，测 A9 静默）、≥1 个公司行动复杂标的（A股送转，对齐 RFC-002）。

| 层 | 用例与选样 | 运行内容 |
|----|-----------|----------|
| PR | **固定 20 例**：6 层 × 3 = 18，+CN·quick、+HK·deep 各 1（CN 语义最复杂、HK 覆盖最弱）。名单冻结于 `eval/pr_sample.yaml`，是否季度轮换 30% 见 [OPEN O4] | cassette 重放 + n_gen=1 + 全部 B 层门禁 + judge×1（L2 维度按 W^PR 宽带门禁） |
| nightly | 90 例/夜（6 层各 15，隔夜轮换，两夜覆盖全量） | n_gen=1、n_judge=3；趋势面板 + flaky 统计 |
| release 前 | 全量 180 例 | n_gen=5、n_judge=3；分维度均值±CI 归档 + 基线对比 |

### 5.2 成本估算

假设（[OPEN O7] 待 M2 实测校准）：quick ≈ $0.10/次生成（sonnet 级，~15k in/2k out）、deep ≈ $0.80/次（opus 级；M4 多智能体后重估）、judge ≈ $0.03/次（haiku 级）；prompt caching 已启用（M2）。

| 层 | 规模 | 估算/次 | 频率 | 月估 |
|----|------|--------|------|------|
| PR | 20 例（10q+10d）×1 gen ×1 judge | ≈ $10 | ~40 PR/月 | ≈ $400 |
| nightly | 90 例 ×1 gen ×3 judge | ≈ $50 | 30 夜 | ≈ $1,500 |
| release | 180 例 ×5 gen ×3 judge | ≈ $490 | 1 次/月 | ≈ $490 |
| 噪声带重测 | 90 例 × k=5 | ≈ $250 | 按 §4.1 触发 | — |

稳态上界 ≈ **$2.4k/月**。降档选项：nightly → 每周 2 次（月估降至 ≈$400）、非 release 层 n_judge=1（再 −15%）。预算上限需维护者裁决 [OPEN O2]。

### 5.3 C2a 冻结 cassette 的录制与存放

- **录制点 = MarketDataProvider 边界**（get_ohlcv/get_fundamentals/get_news/resolve_symbol），而非 tool 边界：技术指标、DCF 等确定性计算在重放时**重新执行**，分析引擎的代码回归才能被评测捕捉（D10）。
- **格式**：键 =（provider, method, canonical_args）；值 = 原始返回 + fetched_at + 许可标签 + SHA-256。每标的一个 json.gz（行情类可 parquet）。
- **manifest（可入公开仓库）**：cassette_version、recorded_at、逐条目哈希清单、provider 版本、E3 许可结论引用——**不含任何原始数据值**，外界可验证完整性而无法重建数据。
- **重放**：`CassetteProvider` 实现同一 provider 接口，严格模式——未命中键即报错，禁止网络 fallback（保证 A 层字节级冻结）。
- **存放（遵守 E3 三选一，本 RFC 推荐 a+b 组合）**：(a) **私有存储为主**——原始 cassette 不入公开仓库，介质 [OPEN O3]；(b) 公开仓库附 recorder 脚本，外部贡献者本地自录（其分数标注「非官方 cassette，不可直接对比」）；(c) 仅存派生指标——会让评测喂不出 LLM 所需的原始事实，**不推荐**单独使用。若 E3 审计认定私有云备份亦构成再分发风险，退守纯本地 + 加密备份。
- **与 A0 的关系**：A0 = 每日增量积累（喂 C2b）；C2a = 一次性录制、版本化冻结。M3 迁入 A3 存储时 manifest 哈希不变，迁移可验证。

## 6. 落地顺序（M1 → M2）

1. M1：本 RFC 评审定稿 → IR spec 冻结（ADR 补录）；E3 出 cassette 存放裁决。
2. M2 实施序：EvidenceStore + verify_constructive（含守门员测试）→ tool 返回改造（内联 eid）→ StructuredReport + 渲染视图 → C2a 录制 → C1 运行器 → 噪声带测定 → C3 门禁演练（含劣化 PR 被拦截演习）→ 正式基线重测归档。

## 7. 决策点汇总

| # | 决策 | 摘要 |
|---|------|------|
| D1 | 证据原子化 | 四类 kind（metric/series_stat/news/doc_quote）；文本类事实同样入 IR 携带溯源 |
| D2 | 派生值供给 | 模块预算集为主 + 纯 eid 白名单 derive tool 为辅；带数值参数的假设类运算 M2 不开放 |
| D3 | tool 返回形态 | 数值旁内联注入 eid；token 开销由 prompt caching 吸收 |
| D4 | 防绕过机制 | R1–R5 机械规则 + 定点重试→丢弃；KPI=管道不变量（守门员测试），健康度=丢弃率 |
| D5 | 唯一事实源 | `report.json` 为准；rich/markdown 均为无状态视图；C1 永不做文本抽取 |
| D6 | 确定性四层 | A 输入/B artifact 评分/C 端到端/D LLM 评审；零容忍=B 层 per-artifact 硬判，不走统计 |
| D7 | 噪声带与检验 | k=5、t 区间半宽 W；门禁=paired 均值回退超 W（nightly/release 加 Wilcoxon 单侧 AND） |
| D8 | 地板配置化 | `eval/gates.yaml` 声明式；zero_tolerance 无豁免，reasoning 可带签字豁免并月报 |
| D9 | 三层评测 | PR 固定 20（6 层分层+2）/ nightly 半量轮换 / release 全量 ×n_gen=5 |
| D10 | cassette 边界与存放 | 录在 provider 边界、严格重放；私有存放 + 公开 manifest 哈希 + recorder 脚本 |

## 8. [OPEN] 汇总

| # | 问题 | 依赖/建议 |
|---|------|-----------|
| O1 | 裸数字白名单边界（年份/季度标签/指标名的豁免范围）与误杀处理策略 | M2 前用真实报告样本实测误杀率再定稿；影响构造性门禁的严格度与报告可用性 |
| O2 | 评测月度预算上限及 n_gen/n_judge/nightly 频率档位 | 粗估稳态 $2.4k/月，降档可至 ~$900/月；需维护者定上限 |
| O3 | cassette 私有存储介质与 CI 接入（本地目录+加密备份 / 私有 git LFS / 对象存储） | 待 E3（M1）许可裁决；涉及法律边界与基础设施成本 |
| O4 | PR 固定 20 例名单是否季度轮换 30% | 防黄金集过拟合（风险 #12）vs paired 可比性与缓存命中的张力 |
| O5 | 完整性维度地板数值 | 建议 ≥0.95 起，按报告类型清单实测后定 |
| O6 | 非 Anthropic 评审模型引入时点 | M2 单族评审可暂接受；C5 公开基准前必须引入（风险 #3） |
| O7 | 成本单价与 token 量假设 | M2 实测校准；O2 的裁决依赖此 |
| O8 | 假设类 derive（DCF 情景改参）机制 | 随 B3 估值引擎设计；需解决「LLM 填参 = 潜在绕过口」 |

---

*本 RFC 为 M0 立项初稿；M1 评审通过后冻结 IR spec 并补录 ADR，schema 演进走 semver（`schema_version`）。*
