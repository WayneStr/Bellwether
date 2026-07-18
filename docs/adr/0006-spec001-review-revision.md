# ADR-0006：spec-001 Evidence IR 双盲深度评审结论与 v1.1 修订

- 状态：Accepted · 日期：2026-07-18 · 决策人：Fable（主编排）综合两路独立评审
- 背景：契约包交付时（2026-07-17 深夜）评审方自留警告「M2 实现前深度复核 spec-001」。M1 实质完成后、M2 动工前执行本次复核。Codex CLI 已从本机消失（HANDOFF §6#8），第二路改为独立 deep-reasoner **红队**视角以保持双盲精神：两路互不可见，主编排结合自己的独立通读做盲综合。

## 评审结论

| 路 | 视角 | 结论 | 计数 |
|---|---|---|---|
| deep-reasoner A | 全维度符合性 | **修订后通过** | BLOCKER 0 · MAJOR 5 · MINOR 11 · NIT 6 |
| deep-reasoner B | 对抗红队（攻构造性保证） | 结构真保证 / **内容纸面保证** | BREACH 11 · WEAK 10 · HELD 7 |

原文：`docs/reviews/2026-07-18-spec001-review-deepreasoner.md` / `-redteam.md`。

**共识（独立撞车，置信度高）**：`Derivation.params` 数值注入通道；「schema 校验 ≠ 构造性已核验」的管道不变量缺失；confidence/降级呈现语义丢失；qfq 复权锚点问题；capture_policy↔pit_class 解耦；fingerprint preimage 未定义。

**关键独有发现**：符合性路——派生/假设类 Evidence 的必填 `SourceRef` 无处可填（schema 自相矛盾）、`Derivation.op` 白名单表达不了技术指标/DCF（主力上报数无法注册）。红队路——**B12** spec-001（`extra="forbid"` 无 anchor_date 字段）与 spec-002 §3（强制记录 anchor_date）两份 Frozen 契约直接矛盾；**B4** 证据必填建立在 LLM 自我声明的 `kind`/`contains_external_facts` 上（「自觉」以 bool 复活）；**B1/B2/B3** value 与源字节无绑定、指针从不被解析、live 路径无可寻址捕获（「provenance 有形状无实质」）；**B10** eid 顺序分配 + 条件式注册 → 回放时静默错绑；**B8** 缓存命中时 captured_at 撒谎。

**正面确认（两路一致）**：对 RFC-000 逐条无偏离；`exact_closure` 三重闭包校验是全 spec 最硬防线；eid 分配/token 语法/available_at 算术/跨市场隔离攻击全部失败（HELD×7）；对 M3/M4 前向兼容良好。

## 裁决

**批准 spec-001 一次性修订至 Frozen v1.1**（本 ADR 即「变更须 ADR」的授权）；**spec-002 不动**（B12 由 spec-001 增 `anchor_date` 字段解决，spec-002 §3 的要求就此落地）。修订组织为三层：

### 1. Schema 修订（§2 Pydantic 规范代码）
- S1 `Evidence.source: SourceRef | None`，校验「source 为 None ⟹ derivation 非 None」；假设类 Evidence M2 不开放，`Scenario.assumption_eids` 仅引用 captured/derived。
- S2 `SourceRef.upstream_source: str | None`：同 provider 降级子源（eastmoney/sina）必填区分，纳入 fingerprint。
- S3 `Evidence.anchor_date: date | None`：`price_basis ∈ {qfq, split_and_dividend_adjusted}` ⟹ 必填（解 B12）。
- S4 `fingerprint` preimage 钉死为语义键哈希（symbol|kind|指标名|period|price_basis|upstream_source，**排除 value 与时间戳**）；cassette/评测路径必填。
- S5 `data_type="news"` ⟹ 禁 `pit_class="authoritative"`；authoritative 仅 filing/白名单权威源。
- S6 kind↔value 类型校验（metric/series_stat ⟹ float；news/doc_quote ⟹ str）。
- S7 `exact_closure` 预检 `derivation.inputs ⊆ evidence`（消 KeyError）+ inputs 拓扑约束（父 eid 序号 < 自身）。
- S8 `Scenario.assumption_eids` 唯一性校验。
- S9 报告级校验：`capture_policy="cassette"` ⟹ 全部 evidence 为 replay；live/silver ⟹ 无 replay。
- S10 不加降级字段：`model_versions` 语义钉死为「实际使用的模型」，与配置主选不一致时渲染器 MUST 输出降级横幅（红线 4）。

### 2. 管道条款（§3 构造性保证闭合——本次评审核心产出）
- P1 管道不变量：`report.json` 仅可由 `verify_constructive` 通过后的管道产出；唯一渲染入口只消费已核验报告；schema-valid 是必要非充分条件。
- P2 新增 R7 溯源解析闭合：每条 source Evidence 的指针 MUST 解析到捕获库条目、`response_sha256` 吻合、`tool_call_id` ∈ 会话 trace；失败即 drop。
- P3 新增 R8 value 忠实性：value MUST 由注册的确定性抽取器从规范化响应产出，verify 用同一抽取器重算比对；翻译/格式化展示串不回写 value；doc_quote 逐字可核。
- P4 live 可寻址捕获：M2 起 live 路径 MUST 持久化每次 tool 响应（capture_id + 规范化字节 + sha256）；`snapshot_ref` 仅文件化快照使用，live 经 capture_id 解析。
- P5 R5 升级为每份报告运行时反查（非仅渲染器 CI 单测）。
- P6 渲染确定性：单一 token 替换器 + 冻结数字格式化规则，rich/markdown 两渲染器共享。
- P7 Claim 判据分段：报告主体段落一律按 fact 判据强制证据；`interpretation` 仅限显式观点段；`contains_external_facts` 只能收紧、不得作为豁免依据（堵 B4）。
- P8 coverage 机械化：每维 status MUST 由实际注册 Evidence 推导（0=missing、部分=degraded）；`reason` 禁裸数字，R1 扫描范围含所有渲染文本。
- P9 `captured_at` = 字节首次真实获取时刻；缓存 MUST 持久化并回填真实捕获时刻；超新鲜度阈值强制 `stale`。
- P10 confidence 呈现语义补回：stale 横幅、estimated 假设连带呈现；`missing` 归 CoverageDimension，Evidence 层不使用。
- P11 措辞诚实化：模糊定量语言（翻倍/三成/由盈转亏…）明文列为**声明式残差**（非构造性覆盖），报告边界声明披露。
- P12 params 栅栏：M2 `derive_metric` 签名仅 `(op, input_eids)`；`params` 仅确定性分析模块可写；带数值参数的假设类运算 M2 不开放。
- P13 被丢弃 claim 的原文与原因 MUST 记入 provenance trace（审计），报告本体保留计数。
- P14 回放错绑守卫：playback MUST 逐条比对录制 eid 的 fingerprint 与重算值，不一致即 fail（堵 B10）。

### 3. 注册约定与贯通（§4）
- M1 技术指标 = `kind="series_stat"`、`derivation=None`、source 指向底层 OHLCV 捕获、`price_basis` 记实际口径；DCF = derived Evidence，`op` 白名单增 `"model"`（仅确定性模块可用，params=假设集，formula 必填）。
- M2 `provenance_ref` 钉死为 trace_id（uuid4 hex）。
- M3 §4 补 trace 贯通：`tool_call_id` = orchestrator 的 `block.id`；M2 需为 ToolCallRecord 增响应哈希与 license 贯通；M1 trace 级 `snapshot_ref` 字段废弃（被 per-SourceRef 粒度取代）。
- M4 snapshot/cassette-backed 捕获的 `canonical_request` 必填非空、经 RFC-000 §8 规范化、时间字段源自 `as_of`。

### 已知限制（明示记录，不阻塞 M2）
`response_sha256`↔`snapshot_ref` 哈希等式待 M3 规范化后收敛；`evidence_ids` 保持与 token 有序全等（规范化顺序，接受 drop 率影响）；SnapshotRef 正则宽松（`{file}` 可含 `/`、日期不验真）；`ClaimKind="scenario"` 保留枚举位、M2 一律用 `Scenario` 对象；price-book 版本由 provenance trace 承载；风险 claim 的放置约定（顶层 `risks[]` 优先）。

## 后果
- spec-001 头部版本 Frozen (M1) → **Frozen v1.1**，修订记录引用本 ADR。
- M2 的 B0/C1/C2a 按 v1.1 实现；R7/R8/P4 意味着 B0 必须先建「live 捕获持久化 + 确定性抽取器」两块地基。
- provider 层配套（B7/B8/B9 的数据侧修复：禁静默 `except: pass`、缓存带捕获时刻、降级链子源标注）属 M2-B0 实现范围，不再单立任务。
- 红队报告的 HELD 清单作为守门员测试（RFC-003 §1.4 R1–R6 + 新 R7/R8）的种子用例。
