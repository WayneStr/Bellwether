# RFC-003: Claim/Evidence IR 与评测体系

| 字段 | 值 |
|------|-----|
| 状态 | **Revised Draft v2（双评审后）· 2026-07-16** |
| 共享契约 | 见 **RFC-000**（SourceRef/snapshot_ref、Evidence 时间语义、EvidenceStore/eid、CoverageReport、tool_call_id、CostLedger、规范化序列化） |
| 关联任务 | B0（IR + report schema + provenance）、C1（评测运行器）、C2a（冻结黄金集）、C3（噪声感知门禁） |
| 里程碑 | M1 冻结 IR spec → M2 落地 |
| 评审依据 | `docs/reviews/2026-07-16-rfc-review-{deep-reasoner,codex}`（DR 3/4/5/7/10/11/14；Codex 1/4/6/11 + 阻断 4） |

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
    tool_call_id: str                    # 关联 trace 中的具体调用（RFC-000 §6 统一键名）
    url: str | None = None               # 新闻/文件类原始链接
    snapshot_ref: str | None = None      # RFC-000 §1 唯一指针（date/run_id/MARKET/SYMBOL/file#sha256）

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
    eid: str                             # "E7"：全会话唯一、从不复用（RFC-000 §4）
    kind: Literal["metric", "series_stat", "news", "doc_quote"]
    value: float | str                   # 数值；news/doc_quote 存原文文本
    unit: str | None = None              # "%"/"倍"/"股"/"元/股"；文本类 None
    currency: str | None = None          # ISO 4217；纯比率/文本类 None
    period: Period | None = None
    # 时间语义直取 RFC-000 §1/§2（废除含混 as_of，Codex 6）：
    valid_at: datetime | None = None     # 经济事件所属时间（RFC-002 D1）
    published_at: datetime | None = None # 源声明发布时间（observed 不可信，仅参考）
    available_at: datetime               # 可用时间 = PIT 判据（RFC-000 §2 推导，禁自由写）
    pit_class: Literal["authoritative", "observed", "replay"]
    price_basis: str | None = None       # 行情类：RFC-000 §1 词表；非行情 None
    source: SourceRef
    derivation: Derivation | None = None # None = 源原始值
    confidence: Confidence
    fingerprint: str | None = None       # 内容寻址哈希，跨运行 paired 对齐用（可选）
```

`EvidenceStore` 为 **session 级单例**（RFC-000 §4：quick 与 deep 共用；eid 全会话唯一、串行分配、`closure` 跨阶段可解析）：`register(...) -> eid`、`get(eid)`、`closure(eids)`（含 derivation 传递闭包）。置信状态渲染约定：`estimated` 必须连带假设呈现；`stale`（超新鲜度阈值）触发报告横幅；`missing` 触发 A9 维度静默。

### 1.3 派生值：一律代码预算，两条供给路径

1. **预算集（主）**：确定性分析模块在 tool 执行时即计算常用派生值并注册——区间收益、同比/环比、估值比率、DCF 敏感性格点。现有 `tools._summarize_ohlcv` 的 `period_return_pct` 即此类，B0 只是补上 Derivation 注册。
2. **`derive_metric` tool（辅）**：LLM 需要预算集之外的运算时，调用 `derive_metric(op, input_eids)`；op 限白名单纯函数（add/sub/mul/div/pct_change/ratio/cagr），代码执行、注册新 Evidence（confidence=derived）并返回 eid。**schema 上 inputs 只接受 eid、不接受数值字面量**——LLM 无法借该 tool 把臆造数「洗」成合法证据。带数值参数的假设类运算（DCF 情景改参）M2 暂不开放，随 B3 估值引擎再定 [OPEN O8]。

### 1.4 证据 ID 引用格式与防绕过

**引用格式**：LLM 产出的叙述文本中，数字位置只允许出现 `[E7]` 式令牌。渲染层将令牌替换为「格式化值 + 单位/币种」（中英本地化随 B8），并自动生成证据附表。渲染替换是最终报告数字的**唯一**来源。

**防绕过 = 核查器 `verify_constructive` 的五条机械规则**（对 LLM 原始输出、渲染前执行）：

| # | 规则 | 拦截的绕过方式 |
|---|------|----------------|
| R1 | 裸数字检测：**token/语法扫描器**（非纯正则，Codex O1）识别阿拉伯数字、中文数字（万/亿）与**中文数量词（翻倍/三成/双位数增长，DR 11）**，除白名单外一律违规。白名单：eid 令牌内部、列表序号、指标名内嵌数字（MA20/RSI14/10-K）、纪年与季度标签（2026 年/FY2025/Q1）、操作性元数据字段（§2.1，日期/成本/版本）。中文数量词**要么纳入检测、要么在边界声明中明文列为残差**（归 C1 推理维度 + 审稿人）；每类白名单配正反例守门员单测 [OPEN O1] | 直接写数 / 用模糊数量词绕过 |
| R2 | eid 存在性：所有令牌必须 ∈ 本次 EvidenceStore | 幻造 `[E99]` |
| R3 | 双写一致：`Claim.evidence_ids` 与 text 中令牌抽取结果一致 | 结构与文本脱节 |
| R4 | 引用闭包：`report.evidence` 恰为引用闭包（含 derivation inputs 传递闭包） | 报告携带无来源证据 |
| R5 | 渲染自证：渲染输出中每个数值可反向映射到某次令牌替换（渲染器单测承担） | 渲染层自己引入数字 |
| R6 | 证据必填（Codex 11）：`kind∈{fact,risk}` 与含外部事实的 `interpretation` 至少 1 个 Evidence；纯模型分析、无外部事实的 interpretation 才可空 | 无来源的文本类事实混入报告 |

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
    # R6 约束（Codex 11）：fact/risk 与含外部事实的 interpretation 必须 ≥1 Evidence；
    #   纯模型分析、无外部事实的 interpretation 才可空。

class Section(BaseModel):
    section_id: str                      # "overview"/"technical"/"valuation"/"events"
    title: str
    claims: list[Claim]

class Scenario(BaseModel):
    name: Literal["bull", "base", "bear"]
    assumption_eids: list[str]           # 假设一律为 estimated 证据
    narrative: str                       # 同样只允许 [E*] 令牌

# CoverageEntry 已废除（DR 4 / Codex 6）→ 全线复用 RFC-000 §5 / RFC-002 D11 的 CoverageReport：
#   status ∈ {available, degraded, missing} + as_of + quality_score + reason

class ReportMeta(BaseModel):
    schema_version: str = "0.1"
    symbol: str
    market: Literal["US", "CN", "HK"]
    tier: Literal["quick", "deep"]
    generated_at: datetime
    analysis_context: dict               # as_of / capture_policy（RFC-000 §3）
    model_versions: dict[str, str]       # 角色 → 模型 id（B9 重定基线的键）
    prompt_versions: dict[str, str]      # 角色 → prompt 版本（DR 7；quick 单键，M4 多角色多键）
    coverage: CoverageReport             # RFC-000 §5 统一结构（原 CoverageEntry 废除）
    dropped_claims: int = 0
    cost_usd: float | None = None
    # 操作性字段（generated_at / cost_usd / schema_version / *_versions）豁免 R1/R5（Codex 11）

class StructuredReport(BaseModel):
    meta: ReportMeta
    sections: list[Section]
    scenarios: list[Scenario] = Field(default_factory=list)
    risks: list[Claim] = Field(default_factory=list)
    evidence: dict[str, Evidence]        # 引用闭包（eid → Evidence）
    provenance_ref: str                  # M2 指向最小 trace manifest（RFC-001 M1/M2 交付）；M4 升级为完整 E4 包
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
| A 输入 | cassette 重放的 provider 输出 | 字节级确定（哈希校验；**规范化序列化见 RFC-000 §8**，防库版本漂移致 flaky） | 评测前提，不出分 |
| B artifact 评分 | 对**某份已生成报告**跑程序化核查（R1–R6、完整性清单、合规规则、schema 校验） | 完全确定，重复评分逐位一致 | 零容忍与地板门禁的判据 |
| C 端到端 | 生成 + 程序化评分 | 生成本身随机（温度 0 也不保证 API 级确定） | 分数是分布；零容忍维度 = 每次每例都必须过 |
| D LLM 评审 | rubric 打分 | 双重随机（生成×评审） | n 次均值 ± 95% CI（t 区间） |

关键推论：**零容忍不与随机性冲突**——事实性/合规是 B 层 per-artifact 硬性质，任何一次运行任何一例违规即红，无需统计。统计只用于 C/D 层的连续分数。

**n 的取值与成本权衡**：方差预期主要来自生成而非评审（待 §4 噪声测定验证），预算优先给 n_gen；judge 用 haiku 级 + rubric + caching，n_judge=3 足以把评审方差压为次要项。建议：PR n_gen=1/n_judge=1；nightly 1/3；release **5/3**（每份报告 3 次评审取均值，再跨 5 次生成取均值±CI）。单次 nightly 只画趋势，不下结论。

## 4. 必答 4：噪声带测定与噪声感知门禁

### 4.1 null 分布测定（同配置重复，非「版本集合波动」）

**废除 `W = t·s`**（Codex 阻断 4）：原式既未除 √k，又拿「同版本集合分数波动」去判「candidate−baseline 的配对均值差」——两者统计含义不同，门禁被放得过宽且不可解释。改为直接标定 **paired 差的 null 分布**：

- **方法**：冻结 main 代码 + cassette + 钉死 judge，在半量集（90 例）跑 **k=5** 次完整端到端评测。null = 同配置两两重复间的**逐例配对差** dᵢ⁰ = scoreᵢ^(a) − scoreᵢ^(b)（以 0 为中心，仅含生成×评审噪声）。保留 5×90 per-case 矩阵。
- **判据统计量**：候选的逐例配对差 dᵢ = candᵢ − baseᵢ 取 **mean(d)**；显著性用 **case 聚类 bootstrap**（按 case 重采样，尊重每例被多次评审的聚类结构）给出 mean(d) 的置信区间。null 分布给出「无真实退化时 mean(d) 的抽样区间」作对照。
- **k=5 的理由**：k=3 bootstrap 尾部不稳；k=10 成本翻倍收益边际。实测 null 不稳时增跑。
- **重测触发**（与 B9 联动）：模型版本 / prompt 主版本 / judge 或 rubric / cassette 版本任一变更 → null 分布与基线强制重测。

### 4.2 paired 门禁：三层只变样本量与置信边界

candidate 与 **baseline-of-record**（钉死模型+prompt+cassette+预算，C5 同源）同题、同 cassette、同 judge。判「配对退化」= mean(d) 的 bootstrap 置信区间**上界 < 0**（即显著为负）：

- **PR 层（20 例）**：90% 单侧区间；样本量小 → 区间宽、只挡明显退化（低误报优先）。
- **nightly（90 例）**：95% 单侧区间；趋势面板 + flaky 统计。
- **release（180 例 ×n_gen）**：95% 区间 + 分维度归档 + 基线对比。
- 三层**同一统计量、同一 null**，仅样本量（→区间宽度）与置信水平不同——**不再有各层单独标定的 W**（消除 Codex 阻断 4 的多带标定）。
- 零容忍维度（事实性/合规）不走此统计：B 层 per-artifact 硬判，任一例违规即红（§3 推论）。
- flaky 率月度跟踪 = 重跑翻绿数/总阻断数，收敛目标 <10%（C3 验收项）。

### 4.3 分维度地板的落地形式

声明式配置 `eval/gates.yaml`，门禁引擎按此执行，任何改动走 PR 评审：

```yaml
dimensions:
  factual:      {gate: zero_tolerance}                  # B 层硬判，任何一例违规即红
  compliance:   {gate: zero_tolerance}
  completeness: {gate: absolute_min, floor: 0.95}       # [OPEN O5]
  reasoning:    {gate: paired_ci, waiver: true}         # bootstrap 置信区间（§4.2）；豁免=PR 标签+签字，月报汇总
composite:      {gate: paired_ci, waiver: false}
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

**联合预算视角（RFC-000 §7 CostLedger）**：本表是月度评测封套，与 deep 生产单价（RFC-001 D6a）、**B2 一次性封套（RFC-001 D14，≤$1,500，单列不混入月度）**共用同一 price-book、同一裁决表。
- 满档稳态上界 ≈ **$2.4k/月**（nightly 每晚为主要项）。
- **降档起步方案（建议默认）**：nightly → **每周 2–3 次** + 非 release 层 **n_judge=1** → ≈**$0.9–1.5k/月**；O7 实测后升档。
- deep 单价 $0.80 是 M4 前假设；若 M4 定标逼近 $1.50（RFC-001 D6a），release 层月估上浮。

金额均标 [OPEN-BUDGET]（已裁决，见 ADR-0004），与 RFC-001 的 [OPEN-BUDGET]（已裁决，见 ADR-0004） **同表联动裁决**。

### 5.3 C2a 冻结 cassette 的录制与存放

- **录制点 = MarketDataProvider 边界**（get_ohlcv/get_fundamentals/get_news/resolve_symbol），而非 tool 边界：技术指标、DCF 等确定性计算在重放时**重新执行**，分析引擎的代码回归才能被评测捕捉（D10）。
- **格式**：键 =（provider, method, canonical_args）；**canonical_args 的时间区间来自 `AnalysisContext.as_of`（RFC-000 §3），录制/重放同一逻辑时钟，杜绝 `datetime.now()` 漂移致重放 miss（Codex 3）**。值 = 原始返回 + captured_at + license_tag + SHA-256（规范化序列化见 RFC-000 §8）。每标的一个 json.gz（行情类 parquet 于 M3 并轨）。
- **manifest（可入公开仓库）**：cassette_version、recorded_at、逐条目哈希清单、provider 版本、E3 许可结论引用——**不含任何原始数据值**，外界可验证完整性而无法重建数据。
- **重放**：`CassetteProvider` 实现同一 provider 接口，严格模式——未命中键即报错，禁止网络 fallback（保证 A 层字节级冻结）。
- **存放（本地加密为主，[OPEN-STORAGE]（已裁决，见 ADR-0004） 待 E3 书面结论）** ← 已采纳（ADR-0004）：(a) **本地目录 + 加密**为主存储，原始 cassette 不入公开仓库、**不用 git LFS**；(b) 公开仓库只放 **manifest 哈希 + recorder 脚本**，外部贡献者本地自录（分数标注「非官方 cassette，不可直接对比」）；(c) 仅派生指标 → 喂不出原始事实，**不单独用**。**CI 边界**：fork PR 拿不到 secrets → **PR 层评测只在维护者机器/自托管 runner 跑**，公开 CI 仅跑 **B 层守门员测试**（播种违规样本，不需原始数据）。**远端加密备份是否允许以 E3（M1）书面结论为准** [OPEN-STORAGE]（已裁决，见 ADR-0004）。
- **与 A0 的关系**（RFC-000 §9 分界）：A0 = 每日增量积累（喂 C2b）；C2a = 一次性录制、版本化冻结；两者共享值序列化 / sha256 / license_tag 惯例但**不合并**（用途不同）。M3 迁入 A3 存储时 manifest 哈希不变，迁移可验证。
- **C2b silver 适配器（归属本 RFC，DR 14）**：M3 提供「从 silver 按 `as_known_at(T, strict)` 喂评测」的 CassetteProvider 兼容适配器，对接 RFC-002 gold 宏与 RFC-000 §2 available_at；C2b 多时点集由此生成。

## 6. 落地顺序（M1 → M2）

1. M1：本 RFC 评审定稿 → IR spec 冻结（ADR 补录）；E3 出 cassette 存放裁决。
2. M2 实施序：EvidenceStore（session 级，RFC-000 §4）+ verify_constructive（R1–R6 + 守门员测试）→ tool 返回改造（内联 eid）→ StructuredReport（`provenance_ref` 指向 RFC-001 M1/M2 交付的最小 trace manifest）+ 渲染视图 → C2a 录制 → C1 运行器 → null 分布测定（§4.1）→ C3 门禁演练（含劣化 PR 被拦截演习）→ 正式基线重测归档。

## 7. 决策点汇总

| # | 决策 | 摘要 |
|---|------|------|
| D1 | 证据原子化 | 四类 kind（metric/series_stat/news/doc_quote）；文本类事实同样入 IR 携带溯源 |
| D2 | 派生值供给 | 模块预算集为主 + 纯 eid 白名单 derive tool 为辅；带数值参数的假设类运算 M2 不开放 |
| D3 | tool 返回形态 | 数值旁内联注入 eid；token 开销由 prompt caching 吸收 |
| D4 | 防绕过机制 | R1–R6 机械规则（R6=证据必填，Codex 11）+ 定点重试→丢弃；KPI=管道不变量（守门员测试），健康度=丢弃率 |
| D5 | 唯一事实源 | `report.json` 为准；rich/markdown 均为无状态视图；C1 永不做文本抽取 |
| D6 | 确定性四层 | A 输入/B artifact 评分/C 端到端/D LLM 评审；零容忍=B 层 per-artifact 硬判，不走统计 |
| D7 | paired null 门禁 | 废除 W=t·s；同配置重复构 null，case 聚类 bootstrap 判 mean(d) 配对退化；三层只变样本量/置信边界（Codex 阻断 4） |
| D8 | 地板配置化 | `eval/gates.yaml` 声明式；zero_tolerance 无豁免，reasoning 可带签字豁免并月报 |
| D9 | 三层评测 | PR 固定 20（6 层分层+2）/ nightly 半量轮换 / release 全量 ×n_gen=5 |
| D10 | cassette 边界与存放 | 录在 provider 边界、严格重放；本地加密存放 + 公开 manifest 哈希 + recorder；PR 评测限维护者/自托管 runner（[OPEN-STORAGE]（已裁决，见 ADR-0004）） |

## 8. [OPEN] 汇总

维护者裁决三类标记：**[OPEN-BUDGET]**（已裁决，见 ADR-0004）（成本，联动 RFC-000 §7）、**[OPEN-STORAGE]**（已裁决，见 ADR-0004）（cassette 远端备份，依赖 E3）。O1/O6 已被两评审共识解决转为决策，其余为设计内待定项。

| # | 问题 | 依赖/建议 |
|---|------|-----------|
| O1（已决方式） | 裸数字白名单边界与中文数量词处理 | **方式已定**（Codex+DR 共识）：token/语法扫描器 + 中文数量词纳入检测或明文声明残差 + 每类正反例守门员单测；具体白名单待 M2 真实报告实测误杀率锁定 |
| **[OPEN-BUDGET]**（已裁决，见 ADR-0004） | 评测月度封套 + n_gen/n_judge/nightly 频率档 | 降档起步 ≈$0.9–1.5k/月、满档 ≈$2.4k/月；RFC-000 §7 单表联动裁决（含 deep 单价、B2 一次性） |
| **[OPEN-STORAGE]**（已裁决，见 ADR-0004） | cassette 远端加密备份是否允许 | 本地加密为主已定；**远端备份依 E3（M1）书面结论**；PR 层评测限维护者/自托管 runner |
| O4 | PR 固定 20 例名单是否季度轮换 30% | 防黄金集过拟合（风险 #12）vs paired 可比性与缓存命中的张力 |
| O5 | 完整性维度地板数值 | 建议 ≥0.95 起，按报告类型清单实测后定 |
| O6（已决） | 非 Anthropic 评审模型引入时点 | **决策：M4**（与 RFC-001 D14 统一，B2 盲评即需；C5 公开基准前必须在位） |
| O7 | 成本单价与 token 量假设 | M2 实测校准；[OPEN-BUDGET]（已裁决，见 ADR-0004） 裁决依赖此 |
| O8 | 假设类 derive（DCF 情景改参）机制 | 随 B3 估值引擎设计；需解决「LLM 填参 = 潜在绕过口」 |

---

*本 RFC 为 M0 立项初稿；M1 评审通过后冻结 IR spec 并补录 ADR，schema 演进走 semver（`schema_version`）。*
