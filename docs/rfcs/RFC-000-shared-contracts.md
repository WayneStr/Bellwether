# RFC-000: 共享契约（Shared Contracts）

> **状态**: Draft v1（双评审后综合产物，与三份 RFC 的 Revised Draft v2 同步）· 2026-07-16 · **契约随 IR spec 于 M1 冻结**
> **作者**: WayneStr（维护者）· 综合: deep-reasoner 与 Codex 两份独立评审的共识项
> **地位**: RFC-001/002/003 的公共接口契约。三份 RFC 与本文冲突时以本文为准；修改本文须做三份 RFC 联动影响评估并录 ADR。
> **依据**: docs/reviews/2026-07-16-rfc-review-{deep-reasoner,codex}.md（接口缝类发现：DR 3/4/5/7/9/10/14；Codex 1/2/3/6/8/12）

---

## 1. ObservationRef / snapshot_ref：唯一数据来源指针

全系统只有一种数据来源指针形态（A0 快照、C2a cassette、trace tool_call、Evidence.SourceRef、report provenance 全部引用它）：

```
snapshot_ref = "{YYYY-MM-DD}/{run_id}/{MARKET}/{SYMBOL}/{file}#{sha256}"
```

- 含 market 与 run-id 段（DR 5 / Codex 2），与 A0 v3 as-built 目录一一对应；从任意引用处可打开原始文件并核验哈希。v1/v2 存量快照缺 run-id 段，导入时以 `run-legacy` 占位。
- cassette 条目以「录制日 / cassette_version」充当日期 / run-id 段，遵同一形态。
- 命名对齐：RFC-001 manifest 的 `data_snapshot_ids[]` → `data_snapshot_refs[]`、tool_call 事件的 `snapshot_id?` → `snapshot_ref?`；RFC-003 的 `SourceRef.snapshot_sha256`（裸哈希不可寻址）→ `SourceRef.snapshot_ref`。

指针解析到的捕获记录 **CaptureRef** 字段表（Codex 2）：

| 字段 | 语义 |
|---|---|
| `capture_id` | 捕获事件唯一 ID（A0 = run_id；cassette = 条目键哈希） |
| `provider_id` + `provider_version` | 数据源标识与包版本（v3 manifest `provider_versions`） |
| `canonical_request` | 规范化请求（method + canonical_args） |
| `response_sha256` | 原始响应哈希（按 §8 规范化序列化后计算） |
| `captured_at` | 捕获时刻（UTC；A0 = manifest.created_at） |
| `valid_at` | 有效时间（经济事件所属时间，RFC-002 D1） |
| `published_at` | 源声明发布时间（可空；observed 源不可信，仅存参考） |
| `available_at` | 可用时间（**由 §2 规则推导，禁止自由写入**） |
| `pit_class` | `authoritative` / `observed` / `replay`（RFC-002 D2） |
| `price_basis` | 行情类必填。capture 级词表（v3 as-built）：`unadjusted` / `split_adjusted` / `split_adjusted_plus_action_columns` / `qfq` / `split_and_dividend_adjusted`；silver 行级仅允许事实口径前三者，视图口径只进对账区 |
| `license_tag` | 许可标签（E3 词表；未裁决前占位 `private-do-not-redistribute (pending E3 audit)`） |

## 2. available_at：前视安全的可用时间（Codex 阻断 1）

`available_at` 是严格 PIT 查询 `as_known_at(T)` 的唯一时间判据。推导规则：

| 情形 | available_at |
|---|---|
| `authoritative` | 权威 `published_at`（如 EDGAR acceptance_datetime） |
| `observed` | **`first_seen_at`**（源 published_at 不可信，仅存参考，不得用于可得性判定） |
| 曾被质量门隔离、后人工放行 | `max(first_seen_at, released_at)` |
| `replay` | 不定义；strict 模式**永远排除** |

**废除 `coalesce(published_at, first_seen_at)`**：它会让 observed 新闻按不可信的源发布时间提前出现，直接违反「只能声称不晚于 first_seen_at 可得」。

## 3. AnalysisContext：逻辑时钟（Codex 阻断 3）

```python
@dataclass(frozen=True)
class AnalysisContext:
    as_of: datetime                                   # 本次分析的逻辑「现在」
    capture_policy: Literal["live", "cassette", "silver"]
    clock: Clock                                      # 注入式时钟；live=系统钟，评测/回放=冻结钟
```

- **M1 起由 CLI 创建并注入**；tool、分析模块、trace、cassette 录制/重放统一从 context 取时间。
- **业务代码禁止直接调用 `datetime.now()`**（守门员测试扫描白名单外的调用点）。评测/回放时 as_of 冻结 → cassette 的 canonical_args 稳定命中。
- 复权 `qfq` 的 `anchor_date` **必须来自 `context.as_of`**（Codex 12）：CLI 缺省行为只是「显式创建 context」，复权函数本身永不读系统时钟。

## 4. EvidenceStore 会话语义（DR 3）

- EvidenceStore 为 **session 级单例**：quick（单 agent）与 deep（多阶段编排）共用同一实例与同一语义。
- **eid 全会话唯一、从不复用**：经单一串行分配器分配（deep fan-out 的并行分析师注册排队进同一分配器），不带 stage 前缀——quick 零变化，deep 无碰撞。
- `closure(eids)` **跨阶段可解析**：任何 stage 的消息引用的 eid，终稿渲染与 verifier 都能解析出完整 derivation 闭包。

## 5. 覆盖矩阵统一词表（DR 4 / Codex 6）

全线采用 RFC-002 D11 的 `CoverageReport`：

```
status ∈ {available, degraded, missing} + as_of + quality_score + reason
```

RFC-003 原 `CoverageEntry`（ok/partial/missing，无质量分）**废除**。`degraded` 语义与「降级不计成功」KPI 对齐。

## 6. tool 调用键统一命名

全线用 **`tool_call_id`**（trace tool_call 事件、`Evidence.SourceRef`、provenance 包）。`tool_use_id` / `call_id` 不再使用；LLM 调用事件主键命名为 `llm_call_id`，与 tool 键不混用。

## 7. CostLedger：三本账合一（DR 6/9 / Codex 8）

三本账**同表分账**，共享同一 **price-book**（ROADMAP-D3 定价表：内置 + 可覆盖 + 版本化）。金额数值均标 [OPEN-BUDGET]，维护者在本表上一次联动裁决（deep 单价直接决定评测与 B2 总价），结论录 ADR：

| 账本 | 性质 | 建议值 [OPEN-BUDGET] | 细则归属 |
|---|---|---|---|
| 生产单次 | 硬上限（调用前置检查，token 与 USD 先到先触发） | quick ≤$0.35；deep ≤$1.50（备选 $1.00/$2.00；自洽性核算见 RFC-001 D6a） | RFC-001 §4 |
| 月度评测（M2 起） | 封套（PR + 周期评测 + release + 噪声重测） | 降档起步 ≈$0.9–1.5k/月，O7 实测后升档（上界 ≈$2.4k/月） | RFC-003 §5.2 |
| B2 受控实验（M4） | 一次性封套，不混入月度 | ≤$1,500（含评审与人工审计） | RFC-001 D14 |

- 阶段/层级「预留」**同时按 token 与美元双计量**（只留 token 时，美元先触顶会使预留失效）。
- 所有成本记账（trace usage、评测报表、实验报告）引用同一 price-book 版本号，跨文档数字可对账。

## 8. 规范化序列化（DR 10）

cassette 值与快照文件在计算 sha256 前必须经规范化序列化，否则哈希随库版本漂移而 flaky：

- **M2（csv/json 先行）**：列序 = schema 声明序；浮点 = 最短往返十进制表示（禁科学计数法）；时间戳 = UTC ISO-8601（`+00:00` 后缀）；JSON 键排序、`ensure_ascii=False`、无尾随空白；CSV 定界符 `,`、LF 换行。
- **M3 并轨 parquet**：固定 pyarrow 版本与写参数（压缩、row group 大小）；逻辑内容哈希以「规范化 csv/json 投影」为准，parquet 物理文件哈希仅作传输完整性校验。pyarrow 依赖不因 cassette 提前进 M2。

## 9. C2a cassette 与 A0 快照：共享约定与分界（DR 14）

**共享三件**：(a) 值的规范化序列化（§8）；(b) sha256/manifest 惯例——逐条目哈希清单 + 版本号 + `_COMPLETE` 式原子完成标记；(c) `license_tag` 词表（E3 裁决，未决前用 §1 占位标签）。

**为何不合并**：用途不同。cassette = 按 `(provider, method, canonical_args)` 键**精确重放**的一次性冻结输入（评测确定性，键空间由调用参数定义）；A0 快照 = **全窗日增**的观测流（时间物理资产，喂 C2b/C4，布局由日历与标的定义）。合并会把 cassette 键空间绑死在快照目录布局上，两边演进互相掣肘。

**C2b 适配器归属**：「从 silver 按 `as_known_at(T)` 喂评测」的 CassetteProvider 兼容适配器归 **RFC-003**（M3 实施，对接 RFC-002 gold 宏的 strict 模式与 §2 available_at 规则）。

---

## 附：三份 RFC 的对齐点索引

| 契约 | RFC-001 | RFC-002 | RFC-003 |
|---|---|---|---|
| §1 snapshot_ref | D9 manifest 与 tool_call 事件 | D5 导入语义、D7 行级列 | §1.2 SourceRef |
| §2 available_at | —（经 tool 层间接使用） | D1 as_known_at、D10 隔离放行 | §1.2 Evidence 时间字段 |
| §3 AnalysisContext | D9 会话 manifest 记录 | D4 qfq anchor | §5.3 cassette 录制/重放 |
| §4 EvidenceStore | §3 消息引用 eid | — | §1.2 |
| §5 CoverageReport | D2 风控输入 | D11（定义处） | §2.1 ReportMeta.coverage |
| §6 tool_call_id | D9 事件 schema | — | §1.2 SourceRef |
| §7 CostLedger | D6/D6a、D14 | — | §5.2 |
| §8 规范化序列化 | —（blob 哈希沿用） | §3 快照哈希 | §5.3 cassette、§3 A 层 |
| §9 cassette/快照分界 | — | D5 bronze 纪律 | §5.3 |
