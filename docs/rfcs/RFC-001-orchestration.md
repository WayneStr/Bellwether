# RFC-001: 多智能体编排与档位

> **状态**: Draft（M0 立项初稿，供评审迭代）
> **作者**: WayneStr（维护者）· 起草协助: Claude deep-reasoner
> **日期**: 2026-07-16
> **关联任务**: B1（编排 v2）、B2（对抗审查受控实验）· 关联决策: ROADMAP §9 决策 12 · 实施里程碑: M4
> **必答题**（ROADMAP §3）: ①每阶段预算绝对上限与降级路径 ②B2 受控实验设计与退出条件 ③trace 回放格式 ④角色图与档位设计

---

## 1. 背景与范围

**现状**：`bellwether/agent/orchestrator.py` 是一个最小单 agent tool-use loop（≤6 轮，5 个数据 tool），模型经 `ModelRouter` 三级覆盖解析（角色 parse/synthesis/deep_report）；`deep=True` 仅切换到 deep_report 角色，无编排、无预算控制、无持久化。

**本 RFC 决定**：deep 档的角色图与消息 schema、两档共用的预算/降级/trace 基建、以及 B2 受控实验的完整设计。**实施在 M4**，但 trace 格式影响 E4（M1 最小版）、实验设计依赖 C2a/B0（M2），故 M0 定稿方向。

**非目标**：agent 间自由对话或动态路由（角色图为静态 DAG）；跨会话 agent 记忆；并发多会话调度；REST/服务化（决策门未开）。

**与其他 RFC 接口**：证据 ID 与 IR 字段以 RFC-003 为准（本文只约定「消息引用 evidence_id」）；tool 输出中的数据快照 ID 语义以 RFC-002 为准。

---

## 2. 档位与角色图

### D1: quick = 单阶段管道，两档共用一套基建

编排核心抽象为 `StageRunner`：执行「一段带 tool 的 LLM 对话」，内建预算计量、超时、重试、trace 录制钩子。

- **quick（默认 `analyze`）** = 单阶段管道：现有 tool-use loop 原样包进一个 stage。行为以 paired 评测对拍确认不变（C3）。
- **deep（`--deep`）** = 多阶段静态 DAG，由图执行器按序/并行调度 stage。

**两档共用**：ModelRouter、tool 层、预算执行器、trace 录制器、确定性 verifier（B0/C1 的 IR 溯源校验）、报告渲染器。**deep 独有**：图定义、各角色 prompt、结构化消息、降级阶梯 L1+。

理由：B2 的退出条件是「保持单 agent + 确定性 verifier」——verifier、预算、trace 必须是两档共有资产，退出时不弃子；共用 StageRunner 使 quick 不需要任何特殊分支即获得 E4 所需的录制能力。

### D2: 角色图（deep 档）

```
规划 ──► 基本面分析师 ─┐
   （并行）技术分析师 ──┼─► 综合·初稿(claim级) ─► 对抗审查 ─► 挑战响应(按域回分析师,可省)
        事件分析师 ────┘                                        │
                                          风控 ◄────────────────┘
                                            └─► 综合·终稿(渲染IR+整合裁决)
```

| 角色 | 模型档(经 Router) | tool 权限 | 输入 | 输出消息 |
|---|---|---|---|---|
| 规划 | parse 档 | 无 | 用户请求 | AnalysisPlan |
| 基本面/技术/事件分析师 | synthesis 档 | 各自域内 tool 子集 | Plan | AnalystReport |
| 综合(初稿) | deep_report 档 | 无 | 3×AnalystReport | DraftIR（claim 级骨架，非成文） |
| 对抗审查(红队审稿人) | deep_report 档 | 只读复查 tool（≤2 轮） | DraftIR + 证据 IR | Challenge[] |
| 挑战响应 | synthesis 档 | ≤1 轮 | 域内 Challenge | ChallengeResponse |
| 风控 | synthesis 档 | 无 | DraftIR+Challenge 裁决+覆盖矩阵 | RiskAssessment |
| 综合(终稿) | deep_report 档 | 无 | 全部上游消息 | FinalReport(IR 渲染) |

要点：
- **上下文隔离**：分析师互不可见；综合只读 AnalystReport 结构化结论，不读分析师原始 tool 转录（控成本 + 缩小注入面，呼应 E5）。
- **初稿是 claim 级 IR + 骨架**而非成文报告：审稿对象紧凑、机器可评分（B2 播种直接在 IR 上做变异），终稿才渲染成文。ROADMAP 角色顺序中「综合撰写」仍在最后，初稿是综合角色的内部前置产物。
- 风控 = 确定性检查（A9 覆盖矩阵、E1 规则、IR 完整性——零 token）+ LLM 风险综合（仅写有证据链的风险/情景块）。确定性部分两档常开。
- 挑战响应仅 1 轮、无再审：审稿人意见 + 分析师回应一并交终稿裁决（终稿须写明采纳/驳回理由入 trace），杜绝审稿-修订循环。
- 规划失败 → 落默认计划（激活全部 3 分析师、标准焦点），不阻断。
- 单个分析师失败/数据缺失 → 该维度显式静默 + 报告横幅（A9 宁缺毋滥），不得由其他角色补写该域结论。
- [OPEN-1] 事件分析师依赖 B6 事件引擎（同在 M4）。若 B6 晚于 B1 就绪，事件分析师首发降级为「新闻 tool 直通 + 仅作背景不产 claim」。是否接受该首发形态需裁决。

### D3: 对抗审查形态——选**红队审稿人**，不选 Bear/Bull 辩论

论证（二选一，ROADMAP §3 要求）：

1. **与 B2 度量对齐**：验收指标是播种错误检出率/误挑战率/错误修正精度——度量单元是「找错」。审稿人的目标函数就是找错；Bear/Bull 辩手的目标函数是论证立场，立场输出需再加一层裁判 LLM 才能折算成「错误修正」，多一级失真与成本。
2. **Goodhart 结构性风险**：Bear 辩手在证据面偏多的标的上**必须**制造看空论点——被制度性激励产出无根据断言，恰是盲评指标二要压降的东西。审稿人被约束为「每条挑战必须引用 evidence_id 或指认缺证据的 claim」，无立场配额。
3. **成本与降级同构**：辩论是双倍对抗开销，且其降级形态（超时→单审稿人）正是审稿人方案本身——直接以降级终态为主路径，主/降路径同构，少维护一套 prompt 与消息类型。
4. **保留复活路径**：若 B2 实验发现审稿人系统性漏检「方向性/论题级」盲区（而非事实级错误），Bear/Bull 作为下一轮受控实验的候选扩张——按 §8 治理「受控实验原则」，须新实验立项，本 RFC 不预留实现。

### D4: 不引入外部编排框架（langgraph 等）

静态 DAG + 预算 + trace 是薄层（估几百行）；框架的核心卖点（动态路由、自主循环）恰是 B2 纪律禁止的方向；且引入编排框架扩大供应链面（风险 #10）。自研 `StageRunner` + 图执行器。

---

## 3. 结构化消息 schema

### D5: 统一信封 + 按阶段强类型 payload（pydantic 校验）

```
Envelope: msg_id, session_id, schema_version, stage, role, ts,
          in_reply_to[], payload_type, payload, cited_evidence_ids[], usage_ref
```

payload 类型（字段为最小集，定稿随 RFC-003 IR 冻结微调）：

| payload | 关键字段 |
|---|---|
| AnalysisPlan | focus_questions[], activate_analysts[], peer_candidates[], data_requirements[] |
| AnalystReport | domain, findings[]{claim_ids, stance, confidence, evidence_ids}, data_gaps[] |
| DraftIR | claims[]（RFC-003 Claim 结构）, skeleton_sections[] |
| Challenge | challenge_id, target_claim_id, type(语义错配/逻辑跳跃/选择性引用/遗漏风险/无根据断言), argument, evidence_ids, severity |
| ChallengeResponse | challenge_id, resolution(accept/rebut/partial), revised_claim_ids |
| RiskAssessment | key_risks[]{desc, evidence_ids}, scenario_ref, coverage_flags, compliance_flags |
| FinalReport | report_ir_ref, adjudications[]{challenge_id, decision, rationale} |

约束：**消息中的数字只能以 evidence_id 引用**（B0 构造性溯源向上游延伸到 agent 间通信，而不仅是最终渲染层）；payload 校验失败 = 该 stage 输出无效，走该阶段降级语义。

---

## 4. 预算与降级

### D6: 双计量硬上限 + 定标协议

计量原语是 token（in+out 累计），成本（USD）按 D3 定价表（内置+可覆盖）折算；**两者取先到者触发**。墙钟独立计。

**定标协议**：下表为初始值（依据：现有 quick 实测轮次外推 + KPI p95 quick≤60s / deep≤5min）；M4 开工时以 C2a 黄金集实测 p95×1.5 重定并写入 ADR，此后改动走配置不改代码。

| 阶段 | LLM 调用 | tool 轮 | token 上限 | 墙钟 |
|---|---|---|---|---|
| 规划 | 1 | 0 | 8k | 20s |
| 每分析师 | 5 | 4（保底 2） | 40k | 90s（三者并行） |
| 综合·初稿 | 1 | 0 | 25k | 45s |
| 对抗审查 | 2 | 2 | 30k | 45s |
| 挑战响应（每域） | 1 | 1 | 15k | 45s（并行） |
| 风控 | 1 | 0 | 20k | 30s |
| 综合·终稿 | 2 | 0 | 35k | 60s |

**会话级硬上限**：deep ≤300k token 或 ≤$1.50（sonnet 档混合折算），墙钟软限 5min（触发快进降级）、硬限 8min（终止并以已有产物渲染残报告）；quick ≤60k token 或 ≤$0.35、墙钟 90s。各阶段上限之和（约 5.6min 最坏串行）有意大于 5min 软限——全阶段同时打满是尾部事件，由软限兜底而非压缩单阶段。
[OPEN-2] deep 单次 $1.50 上限直接决定用户成本预期与 B2 实验总价，需维护者裁决（备选 $1.00 / $2.00 档）。

### D7: 降级阶梯与触发条件

**预留原则**：任意时刻，剩余预算必须 ≥ 下游「必开支出」（综合·终稿 35k + 每个已激活未完成分析师保底 2 轮）。会话启动前置检查：总预算 < 最小可行编排（规划+3×保底+初稿+终稿 ≈ 130k）→ 直接拒绝 deep 并建议 quick，不启动后再烂尾。

削减顺位（从先砍到后砍；确定性 verifier 零成本永不砍）：

| 级 | 动作 | 触发条件 |
|---|---|---|
| L1 | 省略挑战响应轮（Challenge 直交终稿裁决） | 对抗审查阶段超墙钟；或剩余预算 < 必开支出+响应轮预估 |
| L2 | 跳过对抗审查（仅确定性 verifier；报告横幅「对抗审查未执行」） | 审稿调用重试耗尽连续失败；或剩余预算 < 必开支出×1.2；或会话墙钟达 5min |
| L3 | 跳过风控 LLM（确定性检查保留，风险节标注降级） | L2 已触发仍不足；或风控阶段超时 |
| L4 | 分析师轮次 4→2（在 fan-out 时刻判定） | fan-out 前剩余预算 < 全轮次预估×0.8 |
| L5 | 优雅截断：以已完成消息渲染残报告 + 完整降级横幅 | 会话硬上限（token/成本/8min）任一命中 |

- 每次降级写 `budget_event` 入 trace（触发器+计量快照），会话 `orchestration_degraded=true` 单列跟踪（对齐 §6「降级不计成功」精神），降级率上升告警。
- 顺位依据：审稿价值尚待 B2 证明（先砍），风控是已承诺的报告内容（后砍），分析维度宁缺毋滥不静默补写（几乎不砍，砍深度不砍广度）。

### D8: 重试与故障计费

- 单 LLM 调用重试（D2 tenacity，≤2 次退避）**计入阶段预算**——重试不免费，防预算旁路。
- LLM 提供方故障 → Router 级换模型档（D2 降级链），trace 记录 `fallback_from`；换档后价格按实际模型计。
- 预算检查是**调用前置**（输入实测 + max_tokens 预估 ≤ 剩余），不中途掐断已发出的调用。

---

## 5. 会话持久化与 trace 回放

### D9: 存储布局与事件 schema

`~/.bellwether/sessions/{session_id}/`：`manifest.json` + `events.jsonl`（append-only）+ `blobs/`（大 payload 按 sha256 内容寻址，重复上下文自然去重）。quick 与 deep **都全量录制**（E4 覆盖所有报告，非 deep 专属）。

`manifest.json`：session_id、created_at、cli_command、tier、symbol、config_hash、code_version(包版本+git sha)、model_routing(各角色最终 ModelSpec)、prompt_versions(B9)、budget_config、trace_schema_version、data_snapshot_ids[]、status、totals{tokens_in/out, cost_usd, wall_ms}、degradations[]。

`events.jsonl` 信封 `{seq, ts, type, stage, payload}`，type：

| type | payload 关键字段 |
|---|---|
| llm_call | call_id, role, model, params, prompt_version, **request_blob**(完整请求序列化), **response_blob**, stop_reason, usage{in,out,cache_read/write}, cost_usd, latency_ms, attempt, fallback_from? |
| tool_call | tool_use_id, name, input, output_blob, provider, as_of, snapshot_id?(RFC-002), cache_hit, latency_ms, error? |
| msg | §3 结构化消息信封全文 |
| budget_event | kind(degrade/reserve/exceed), from_level, to_level, trigger, meter_snapshot |
| stage_start/end | stage, budget_alloc / budget_spent |
| session_start/end | 配置快照 / 最终状态与合计 |

决策：**请求全文与响应都落盘**（而非只存哈希靠代码重组）——回放不依赖「组装代码永不变」这一脆弱假设；内容寻址去重使磁盘成本可忽略（deep 会话约 1–2MB 文本）。

### D10: 回放语义——trace playback 的严格定义

**回放 = 不重新执行任何 LLM 调用与 tool 调用**，按 seq 取录制的 response/output 重演；重放器只重新执行确定性部分（消息组装、IR 构建、verifier、渲染）。与 E4 定义完全一致：「重放已录制的 LLM 输出」。重新用活模型跑一遍叫 **re-run**，是另一命令、不共享「可复现」承诺。

`bellwether replay <session_id>`：
- `--verify`（默认）：逐调用重组请求并与录制 request_blob 哈希比对（组装确定性检查）；不匹配 → 标记 **stale**（代码已漂移），仍可 `--render-only` 出报告但不得声称一致性。
- 验收判据（对应 B1「deep 分析可完整回放」）：重渲染报告与原报告 IR 级一致（字节级为目标，IR 级为门禁——渲染层小改不应击穿回放承诺）。

### D11: 与 provenance 包（E4）及 E3 的关系

**一套录制，两个消费者**：E4 provenance 包 = 本会话 manifest + events + 被引 blobs (+ 报告与 IR) 的自包含导出（`bellwether provenance export <id>`），其要求的输入哈希/数据快照 ID/模型与 prompt 版本/完整 tool 记录全部来自上表字段，不另建采集管道。trace_schema_version 随 manifest 演进，旧会话迁移策略对齐 RFC-002 的快照迁移纪律（未知字段保留、只加不删）。
**E3 约束**：tool_call output_blob 含原始行情/新闻数据，provenance 包**默认仅本地**；导出/分享路径给许可提示，遵守 `data-licensing.md` 裁决。

---

## 6. B2 受控实验设计

### D12: 两段式——先审稿人单元实验（廉价、作闸门），后三臂盲评（昂贵）

前提：C2a 冻结 cassette 保证各臂 tool 输入完全一致；B0 IR 已落地；prompt caching 已启用（M2）。方法先于运行冻结：`docs/experiments/B2-preregistration.md`（对齐 C4「评分方法先于数据锁定」）。

### D13: 实验一——播种错误检出（审稿人 standalone）

**关键设计约束**：播种错误必须落在**确定性 verifier 够不到的残差错误空间**。数值抄错/单位错链在 B0 下是构造性防杀的——拿它们播种是在测一个免费组件已解决的问题，虚增审稿人价值。播种类别（与 Challenge.type 对齐）：
①语义错配（引用合法证据但期间/口径/主体错，如以 Q1 证据支撑「全年」论断）②逻辑跳跃（证据真实但不支撑结论）③选择性引用（忽略同源相反证据）④遗漏重大风险 ⑤定性无根据断言。

**构造**：对黄金集 90 标的各生成一份 DraftIR，程序化+人工变异注入 2–6 个错误/稿（数量随机、审稿人不知情）；另置 20%（18 稿）**零错误对照稿**。种子池 60/40 切分为 dev/held-out，防对种子集过拟合（风险 #12）。

**指标与建议阈值**：
- 检出率 **X ≥60%**（①类单独 ≥75%）。理由：人类同行评审对植入缺陷的检出率文献区间约 30–60%；播种错误是「已知类别、刻意构造」，比野生错误易检，故门槛取人类区间上沿——达不到 60% 的审稿人不值它的 token。①类更接近机械可查，单列高线。
- 误挑战率 **Y ≤20%**（挑战未命中任何播种错误、且人工裁决为无根据 ÷ 挑战总数）；零错误对照稿上人均误挑战 ≤1 条。理由：每条误挑战消耗响应轮预算并可能把正确内容改坏（Goodhart 反噬路径）；1/5 以下人类编辑仍会信任审稿人，超过 ~30% 审稿意见退化为噪声。
- 判「检出」标准：challenge 指向被播种 claim 且论证实质指向该错误（边界情形人工复核）。
- [OPEN-3] X/Y 具体取值（60/20）是建议值，依据是跨领域类比而非本域数据，需评审裁决或先跑 dev 集校准后锁定 held-out 门槛。
- **迭代规则**：不过闸 → 审稿人设计（prompt/模型/上下文）至多迭代 2 轮，只在 dev 集调、held-out 终判；两轮仍不过 → 放弃对抗审查阶段，deep = 无审稿图（直接进 D15 退出分支），实验二不再花钱。
- 成本：~108 稿 × k=2 次审稿 ≈ 220 次调用 × ~30k token ≈ **$60–100**。

### D14: 实验二——三臂盲评

**臂设置**（同标的、同冻结 cassette、paired）：
- **Arm1** 单 agent + 确定性 verifier（quick 现状 + verifier，即退出条件的保底形态）
- **Arm2** 全编排减对抗审查（planner+分析师+风控+综合）
- **Arm3** 全编排含对抗审查

三臂而非两臂的理由：退出条件要求裁决「编排整体」与「审稿阶段」两个独立问题；两臂（Arm1 vs Arm3）测不出增益归属——若增益全来自多分析师，砍掉的应是审稿人而非整个 deep。3vs2 = 审稿边际价值（Goodhart 敏感指标主战场），2vs1 = 编排本身价值，3vs1 = 对外 headline。
[OPEN-4] 三臂比两臂贵 50%；若预算收紧，退化方案是两臂（1 vs 3）+ 放弃归因，砍角色时只能整砍。需裁决。

**三指标测量方法**：
- **错误修正精度**：人工+verifier 审计 Arm2 报告中的真实错误（基数），核对 Arm3 对应报告修正比例；同时统计 Arm3 相对 Arm2 的**新增退化**（改坏的正确内容），净精度 = 修正 − 退化。主指标。
- **无根据断言下降**：机械部分 = IR 中 evidence_ids 为空的定性 claim 计数（构造性可查）；语义部分 = LLM 评审判「引用不支撑断言」+ 人工抽检 20%（C6 一致性校准适用）。
- **遗漏补回率**：按黄金集标注的每标的重大事项清单（冻结窗口内已知重大事实/风险），统计各臂覆盖比例。清单标注是新增人工工作量，计入 M4 估算。
- 盲评协议：评审（人工 + ≥1 非 Anthropic LLM 评审，呼应 C5）看去溯源、随机顺序的成对报告。

**样本量与统计**：n=90 标的（全黄金集，保留分市场切片）× k=3 次重复取均值 × 3 臂 = **810 次 deep 级运行**；成本上界 810×$1.50=$1,215，实测预期 $600–900（caching + 通常低于上限），加评审与人工审计，**实验总预算 ≤$1,500**。paired 差值 bootstrap 95% CI（按标的重采样）；n=90、k=3 下最小可检测效应约 0.25–0.3 SD。预注册主指标 = 错误修正精度(3vs2)，其余为次要（Holm 校正）。
[OPEN-5] 若 $1,500 超预算，备选 n=45（15/市场）×k=3，MDE 退到 ~0.4 SD 且丧失分市场功效。与 [OPEN-2][OPEN-4] 联动裁决。
[OPEN-6] 误挑战/错误审计的人工裁决工作量估 2–4 人日（90 标的 × 数条挑战 + Arm2 错误审计），单人维护者是否自任裁决员（自我偏差）或引入外部评审，需裁决。

### D15: 退出条件（预注册，不得事后放宽）

| 结果 | 动作 |
|---|---|
| 实验一两轮不过闸 | 放弃对抗审查；实验二改测 Arm1 vs Arm2 |
| 3vs2 主指标 CI 含 0 或净增益 <10pp | deep 图砍掉审稿阶段（保留 L2 形态为常态） |
| 2vs1 与 3vs1 均无显著净增益（主指标及全部次要指标） | **保持单 agent + 确定性 verifier**；`--deep` 不发布或仅留 experimental 标志；角色图冻结 |
| 3vs2 显著但综合评测分（C1）回退超噪声带，或成本失控（>$1.50 上限常态命中） | 视为无净增益，同上行处理 |
| 显著净增益 | deep 按 Arm3 发布；**任何进一步角色扩张须新的受控实验立项**（含 Bear/Bull 复活） |

实验报告归档 `docs/experiments/`，扩张与否引用数据（B2 验收）。

---

## 7. 验收映射与实施切分（M4 内）

| B1/B2 验收 | 本文机制 |
|---|---|
| deep 分析可完整回放 | D9 全量录制 + D10 playback 定义与 --verify |
| 预算超限优雅截断 | D7 预留原则 + L5 残报告渲染 + budget_event 留痕 |
| B2 实验报告归档、扩张有数据依据 | D12–D15 预注册 + 退出条件表 |

实施顺序（每步带验证）：
1. StageRunner 抽取 + quick 包装 → paired 评测对拍 quick 行为不变；
2. trace 录制器 + replay CLI → quick 会话回放 IR 级一致（此步同时交付 E4 最小版地基）；
3. 图执行器 + 消息 schema + 预算执行器 → deep 全图跑通；故障注入演练 L1–L5 各触发一次；
4. 实验一 harness 与种子池 → 闸门判定；
5. 实验二 + 报告归档 → D15 裁决。

---

## 8. [OPEN] 汇总

| # | 问题 | 建议默认 |
|---|---|---|
| OPEN-1 | 事件分析师在 B6 未就绪时的首发形态（新闻直通不产 claim） | 接受降级首发 |
| OPEN-2 | deep 单次成本硬上限取值（$1.00/$1.50/$2.00） | $1.50 |
| OPEN-3 | 播种实验 X/Y 阈值（60%/20%）的最终锁定方式 | dev 集校准后锁 held-out |
| OPEN-4 | 实验二三臂 vs 两臂（归因能力 vs 省 50% 成本） | 三臂 |
| OPEN-5 | 实验二样本量 n=90 vs n=45（联动 OPEN-2/4） | n=90 |
| OPEN-6 | 人工裁决员：维护者自任（有自我偏差）vs 外部评审 | 维护者 + 争议样本外部复核 |
