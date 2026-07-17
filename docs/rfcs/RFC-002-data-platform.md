# RFC-002 · 数据平台与点对时（PIT）存储

| 项 | 值 |
|---|---|
| 状态 | **Revised Draft v2（双评审后）· 2026-07-16** |
| 共享契约 | 见 **RFC-000**（snapshot_ref / available_at / AnalysisContext / CoverageReport / price_basis 词表） |
| 关联任务 | A0 · A1 · A3 · A4 · A5 · A7a · A9（M1–M3 实施；本 RFC 为 M0 立项） |
| 关联文档 | ROADMAP §3 必答题 / §4 WS-A；docs/reviews/2026-07-16-rfc-review-{deep-reasoner,codex}（DR 2/4/5/8；Codex 1/5/10）；as-built = ADR-0003 + snapshot.py v3 |
| 治理 | RFC 过评审后方可动代码（ROADMAP §8）；裁决后补录 ADR |

---

## 0. 背景、现状与范围

**P0 数据层与本 RFC 直接相关的四项债务**：

1. **复权污染**：`yfinance_provider.py:29` 用 `auto_adjust=True`，`akshare_provider.py:123/181` 用 `adjust="qfq"` —— 缓存与分析链路流通的全是「以取数当日为锚」的复权序列。
2. **无时间语义**：仅有 `fetched_at`（取数时刻），无发布时间 / 获知时间之分，无法支撑任何 PIT 声明。
3. **缓存即存储**：`.cache/*.pkl`（`cache.py`）是唯一持久层，TTL 过期即丢，无 append-only、无来源、无哈希。
4. **单源无对账**：每市场一个 provider，坏数据无拦截直达分析。

**范围**：双时间 schema 与 PIT 判定规则；原始价+公司行动存储与复权引擎边界；A0 快照迁移与向后兼容；DuckDB+Parquet 表设计与分区；质量门（A4）与覆盖矩阵（A9）接口。
**非范围**：A1 协议的 async/取消语义细节（另行设计，本文只定其与存储的接口）；EDGAR 分块索引与 RAG（B7）；证据 IR 字段（RFC-003）；专业源适配（A6）；评测 cassette 存放策略（E3 裁决）。

---

## 1. 必答一：双时间语义与 PIT 判定

### D1 —— 每条记录三个时间字段，append-only

采用 bitemporal 模型 + 观测轴，所有 silver 层表统一带：

| 字段 | 语义 | 按数据类型的具体含义 |
|---|---|---|
| 有效时间（valid） | 经济事件所属时间 | 行情=trade_date；财报=period_end；新闻/公告=事件发生时点 |
| `published_at`（可空） | **源声明**的发布/可得时间 | EDGAR=acceptance_datetime；公告=披露时间；新闻=源标称时间（不可信，仅存参考） |
| `first_seen_at`（必填） | **本系统**首次观测到该记录的时刻 | A0 快照=manifest.created_at；M3 摄取=ingest 时刻 |

- 所有表 **append-only，禁止 UPDATE/DELETE**。同一有效时间键的数据变化（源修正、财报重述、拆股回溯重写）→ 插入新版本行（新 `first_seen_at`），旧行永存。
- 查询语义两种：`latest`（每键取 `first_seen_at` 最新版）；`as_known_at(T)`（`available_at <= T` 中取最新版，`available_at` 按 **RFC-000 §2** 推导——**废除 `coalesce(published_at, first_seen_at)`**，它会让 observed 新闻按不可信源发布时间提前出现，Codex 阻断 1）。财报重述由此自然建模：同一 period_end 多版本，T 时刻自动看到当时最新一版。
- `first_seen_at` 是 observed 源可得时间的**保守上界**（数据在被我观测时必然已可得）——晚估不早估，不会制造前视；故 RFC-000 §2 规定 observed 的 `available_at = first_seen_at`。

### D2 —— PIT 三级分类与判定规则

行级字段 `pit_class ∈ {authoritative, observed, replay}`，**入库时判定、永不事后升级**（历史观测证据不可补造）：

| 级别 | 判定条件（全部满足） | 可宣称 | 典型源 |
|---|---|---|---|
| `authoritative` | 源提供权威时间戳 + 发布后原文不可变 + 第三方可独立核验（官方存档） | 「T 时刻市场已知」 | EDGAR filings（A7a，acceptance_datetime 秒级） |
| `observed` | 本系统有落盘观测证据（bronze manifest + sha256） | 「不晚于 first_seen_at 可得」 | A0 起每日快照的行情/基本面/新闻；M3 后增量摄取 |
| `replay` | 两者皆无（一次性回填的历史序列） | 仅「历史回放」，**禁止用于零前视声明** | 回填 OHLCV 历史、yfinance 财务历史（无 filing 日期） |

- 免费源新闻（yfinance news、东财新闻）自带发布时间但**源可改、不可核验** → 一律 `observed`，源时间存入 `published_at` 仅作参考、`available_at = first_seen_at`（RFC-000 §2）；**新闻与基本面观测是不可回填资产**，这正是 A0 每日观测「买不回来」的原因（DR 8c：**A股/港股公司行动可由公告回填、属 replay 级**，不在此紧迫性内）。
- 严格 PIT 查询模式（供 C2b/C4）：`as_known_at(T, strict=true)` 以 `available_at` 为判据并自动排除 `replay` 行（RFC-000 §2）。
- A1 协议 v2 的能力声明须含 `pit_support` 字段（provider 自报能否提供权威发布时间），存储层据此打 `pit_class`，不信任 provider 的自我声明超出该范围。

---

## 2. 必答二：存原始价 + 公司行动，复权现算

### D3 —— 事实与视图分离：复权序列一律不落库

**论证（为何不存复权后序列）**：

1. **全序列重写 vs 审计**：qfq 以最新价为锚，每次分红/送转使整条历史序列数值改变。若落库，「2026-07 写入的 2020 年收盘价」会在下次除权后变掉——append-only 失效，快照哈希对不上，「抽检零前视」（A3 验收）无法证明。
2. **假前视注入**：今天的复权因子含未来公司行动信息；用它折算历史价格 = 把未来信息写进历史。凡依赖价格水平的逻辑（涨跌停复核、整数关口、历史新高/新低）在回测中被未来污染。
3. **A股尤甚**：高送转频繁（10送10 → 价格腰斩）；长期高分红股 qfq 可出**负价格**；hfq 数值无价格直觉；且东财/新浪的复权算法与锚点互不一致——存复权序列等于存了「某源某日的算法输出」，不是市场事实。
4. **跨源对账不可能**：raw+actions 可逐日双源对账（A4 的地基）；复权序列因源因日皆异，无对账基准。

**结论**：存储层只存两类事实——原始成交价（`bars_daily`）+ 公司行动事件（`corporate_actions`）。复权序列是事实的确定性函数（视图），按需现算。

**现实口径修正（`price_basis`）**（DR 8，对齐 RFC-000 §1 词表）：Yahoo 免费源的 Close 是**拆股回溯调整值**（真未调整价不可得），US 事实层标 `split_adjusted_plus_action_columns`（携 dividends/splits 列）；akshare `adjust=""` 是真原始价，标 `unadjusted`。silver 事实表**只收事实口径**（`unadjusted` / `split_adjusted` / `split_adjusted_plus_action_columns`），视图口径（`qfq` / `split_and_dividend_adjusted`）只进对账区。复权引擎按 basis 选因子集（split_adjusted 只需叠加分红因子）。源在拆股日重写历史 → D1 版本化把该重写记录为新版本行，可检测、可审计。

### D4 —— 复权引擎边界（纯函数）

```
adjust(bars, actions, mode, anchor_date, price_basis) -> bars_adj   # 无 IO，可单测/property-test
```

| mode | 锚 | 用途 | 约束 |
|---|---|---|---|
| `none` | — | 存储原值、事实展示 | 默认 |
| `hfq` | 首个交易日 | **回测收益/技术指标默认口径** | 因子只追加、历史不重写 |
| `qfq` | **anchor_date 必须来自 `AnalysisContext.as_of`**（RFC-000 §3；复权函数永不读系统钟，Codex 12） | 对照行情软件、用户展示 | 报告中 qfq 价必须标注 anchor，保证可复现 |

- A股因子：除权参考价 =（前收盘 − 每股现金红利 + 配股价×配股比）/（1 + 送转比 + 配股比）；hfq 因子 = 前收盘 / 除权参考价，累乘。US/HK 为 split + dividend 因子（HK 另有供股，按配股同式）。对账 ground truth = **交易所公式自算**（OPEN-2 已决，东财 qfq 仅回归对拍参考）。
- 验证接口（**复权正确性回归，非跨源对账**）：用本引擎重算 `qfq(anchor=context.as_of)`（RFC-000 §3）与源 qfq 序列逐日对照，相对误差容差初设 ≤1e-3（除权日 ≤1e-2），实测后校准。

### D12 —— A0 快照 = raw 事实层 + 复权视图双抓（**v3 as-built**）

> **as-built**：本节描述 `snapshot.py` **已落地的 v3 实现**（ADR-0003 + M0「A0 写入加固」提交），非变更请求。M3 导入器按此契约实现——**语义角色以 manifest 的 `price_basis` 为准、不看文件名**（DR 2 / Codex 5，防 qfq 灌入事实表）。

每标的双抓两个文件：

| 文件 | adjust | 语义角色 | price_basis（US ／ CN·HK） | 去向 |
|---|---|---|---|---|
| `ohlcv.csv` | default | **复权视图**（连续性展示） | `split_and_dividend_adjusted` ／ `qfq` | **对账区（quality 库）**，永不进 silver 事实表 |
| `ohlcv_raw.csv` | raw | **事实层** | `split_adjusted_plus_action_columns` ／ `unadjusted` | **silver `bars_daily`** |

- **US 事实层**携带 dividends / stock splits 列（`actions_captured=true`），即公司行动的第一批原始观测；因 Yahoo 已做拆股回溯，其 price_basis 如实标 `split_adjusted_plus_action_columns`（非真未调整价）。
- **CN/HK 事实层**为真未复权价（`unadjusted`）但**暂无 actions**（`actions_captured=false`，manifest 记 `actions_note`：公告可回填、M3 补）；无独立 `ohlcv_qfq.csv`——CN/HK 的复权视图就是 `ohlcv.csv`（qfq），对账样本直接取对账区。
- **写入不可变性**：run-id 目录 `{date}/{run_id}/` + 全部文件与 manifest 写完才落 `_COMPLETE` 原子标记（读取者只认含 `_COMPLETE` 的 run），同日多次运行不覆盖。`manifest.json` = **v3**（`schema_version=3`、`run_id`、`provider_versions`、`license_tag`、entry 级 `price_basis`/`actions_captured`）。

---

## 3. 必答三：A0 快照的 schema 迁移策略

### A0 快照布局（as-built，v1 → v3）

**v3 现状**（导入器主目标形态）：

```
~/.bellwether/snapshots/{YYYY-MM-DD}/{run_id}/
  _COMPLETE                          # 原子完成标记：只认含它的 run
  manifest.json                      # v3: schema_version=3, date, run_id, created_at, smoke,
                                     #   provider_versions{yfinance,akshare}, license_tag,
                                     #   entries{"US:AAPL": {market, files{ohlcv|ohlcv_raw|
                                     #     fundamentals|news:{path,sha256,bytes,rows/count,adjust}},
                                     #     errors{}, price_basis{ohlcv,ohlcv_raw}, actions_captured,
                                     #     actions_note?}}, failures{}
  {MARKET}/{SYMBOL}/ohlcv.csv · ohlcv_raw.csv · fundamentals.json · news.json
```

- **版本差异**（导入器 per-version reader 各读一版，D6）：v1（`schema_version=1`，仅 2026-07-16 smoke）无 run_id 层、无 ohlcv_raw、manifest 无 provider_versions/license_tag/price_basis、files 平铺；v2（ADR-0003）加 ohlcv_raw 仍无 run_id 层；v3 = run_id 目录 + `_COMPLETE` + manifest v3 字段（Codex 5：manifest 为 entries/failures **嵌套**，非平铺）。

### D5 —— 快照 = 不可变 bronze 层；迁移 = 重放导入，永不「转换后弃原件」

- 快照文件**永不改写/删除**（介质搬迁允许，须逐文件 sha256 校验）。
- M3 的 DuckDB+Parquet 是**派生层**：`bw data import-snapshots` 按日期序重放 bronze → silver。任何未来 schema 演进（M3 之后亦然）都通过「升级重放器 + 重建派生层」完成——早期快照天然免疫演进，这是「活过 M3」的结构性保证，而非逐版本写转换脚本。
- 导入语义：`first_seen_at = manifest.created_at`；`pit_class = observed`、`available_at = first_seen_at`（RFC-000 §2）；行级 `snapshot_ref = "{date}/{run_id}/{MARKET}/{SYMBOL}/{file}#{sha256}"`（**RFC-000 §1**，含 market 与 run-id；v1/v2 存量用 `run-legacy` 占位），从任意 silver 行可回溯原始快照文件（供 E4 溯源与 A3 验收抽检）。
- **视图/事实分流**（DR 2 阻断收口）：`ohlcv.csv`（复权视图）→ 对账区（quality 库）供 A4 对拍；`ohlcv_raw.csv`（事实层）→ silver `bars_daily`——以 manifest `price_basis` 判角色，防 qfq 灌入事实表。fundamentals.json / news.json 全量转 `observed` 事实。

### D6 —— 版本化 reader 契约（向后兼容的机械保证)

- `schema_version` 标注**快照布局与文件格式**；只允许加法演进（新增文件/字段，旧 reader 忽略未知项）；删除/改名 = 开新 major。
- 导入器实现为 **per-version reader 注册表**（`SnapshotReaderV1`、`V2`…），旧版本 reader **永久保留、永不删除**。
- CI 固定一份 v1 微型 fixture（3 标的 × 1 日，经 E3 许可安全化处理）跑导入回归：**「任何 commit 都能读回 v1 快照」是硬门禁**。
- 迁移验收（对 ROADMAP A3「A0 早期快照完整迁移」）：全量导入报告 = 逐文件哈希校验通过率 100% + 行数对账 + 抽样回溯 snapshot_ref 可打开原文件。

---

## 4. 必答四：DuckDB + Parquet 存储选型细节

### 分层架构

```
bronze  原始响应（文件系统，不可变，manifest+sha256）   ← A0 快照 / M3 摄取落盘
silver  规范化双时间表（Parquet，append-only）          ← 重放导入 / 每日增量
gold    DuckDB 视图与宏（latest / as_known_at / 复权）  ← 分析与评测的唯一入口
```

### D7 —— 表设计：四类数据 × 三市场 = 同 schema，market 作列 + 分区键（不分表）

理由：三市场 schema 同构（差异用可空列，如 CN 的 turnover/涨跌停），跨市场查询（组合分析）无需 UNION 拼表。

| 表 | 逻辑主键 | 关键列（除 D1 三时间字段与 market,symbol,source_id,pit_class,snapshot_ref 外） |
|---|---|---|
| `bars_daily` | market,symbol,trade_date,source_id,first_seen_at | open/high/low/close（原值）,volume,turnover?,currency,`price_basis` |
| `corporate_actions` | market,symbol,action_type,ex_date,first_seen_at | action_type(cash_div/split/bonus/transfer/rights/…),cash_amount,shares_multiplier,rights_price,rights_ratio,record_date,pay_date；published_at=公告日 |
| `fundamentals_snapshot` | market,symbol,first_seen_at | 观测型指标（TTM PE/PB/市值…，对齐现 FundamentalData 宽列）+ extras MAP [OPEN-4] |
| `fundamentals_period` | market,symbol,statement,period_end,first_seen_at | 报表科目,fiscal 口径,currency；published_at=filing 日（EDGAR→authoritative；回填→replay） |
| `news` | market,symbol,news_id,first_seen_at | title,url,published_at（源值,不可信）,summary；news_id 去重键见 [OPEN-5] |
| `filings_meta`（A7a） | accession_no | form_type,filed_at（authoritative）,period_of_report,primary_doc,blob_sha256 |

- **filings 原文**：content-addressed blob 区 `blobs/{sha256前2位}/{sha256}`，不可变；Parquet 只存元数据+指针（页/段锚点索引属 B7 范围）。
- **A5 基础表**（小表，DuckDB 内或单 parquet）：`symbols`（含 listed_at/delisted_at，**退市标的永不删除** → 无幸存者偏差）、`symbol_changes`（代码变更链）、`trading_calendar`（三市场，喂 A4 缺口检测）、`industry_class`（自由分类，标注 source+as_of，非官方 GICS）。

### 分区与写入策略

- Hive 分区：`bars_daily`、`fundamentals_*` 按 `market=?/year=?`；`news`、`filings_meta` 按 `market=?/month=?`（量大、时间局部性强）；`corporate_actions` 与 A5 小表不分区。
- **symbol 不作分区键**（90 → 数千标的会碎片化）；分区内文件按 (symbol, 有效时间) 排序，靠 DuckDB zone-map 下推。
- 写入：每日增量追加一个 parquet 文件；**月度 compaction** 合并小文件至 ~128MB 目标——只重排不改行，snapshot_ref 与行内容不变，审计无损。

### D8 —— DuckDB 只当引擎与目录，Parquet 是持久格式

`catalog.duckdb` 仅存视图/宏定义，可由 `bw data rebuild-catalog` 从 parquet 全量重建——消除 DuckDB 文件格式跨大版本锁定风险。核心宏：

- `bars_latest(market, symbol)` —— 每 trade_date 取最新版本；
- `bars_as_known(market, symbol, T, strict)` —— D1 语义，判据 `available_at`（RFC-000 §2）；strict=true 排除 replay（供 C2b/C4）；
- `bars_adjusted(market, symbol, mode, anchor)` —— 调用 D4 复权引擎（Python 层实现，DuckDB UDF 或取回后计算，实施时定，不影响接口）。

### D9 —— 缓存降级为纯性能层

silver 是唯一事实库（system of record）。现 `.cache/` 降级为：(a) 网络响应短 TTL 缓存（防重复请求，配合 A1 限流声明）；(b) 复权/指标 memoization（key 含 actions 集合哈希 → 公司行动更新自动失效）。**缓存可随时全删，不承担任何正确性职责**。取数顺序：silver 覆盖且新鲜 → 直接用；否则 provider 拉取。在线拉取 **M3 不 write-through 入 silver**（OPEN-3 已决；Codex immutable bronze staging 记为未来形态）；M3 先做「A0 每日批量 + 显式 `bw data backfill`」两条入库通道。

---

## 5. 必答五：质量门（A4）与覆盖矩阵（A9）的接口

### D10 —— 质量门：四层检查、批次隔离、append-only 更正

作用点：bronze → silver 导入的每个批次（(market, symbol, data_type, batch)）。

| 层 | 检查 | 失败处置 |
|---|---|---|
| L1 结构 | schema/类型/非空/重复键 | **一票否决**，整批隔离 |
| L2 单序列 | OHLC 不变式（low≤min(o,c)≤max(o,c)≤high, vol≥0）；对照 trading_calendar 的缺口；无解释跳变（\|Δclose\| 超阈且无对应 action / 涨跌停解释，CN 用 10%/20% 制度先验） | 计入质量分 |
| L3 跨源 | **M3 每市场仅一源 → 标 `not_assessed`，不计质量分**（Codex 10）。同源 qfq 对拍（复权重算 vs 源 qfq，D4）**不算跨源对账**，仅作复权正确性回归；真跨源验收移至第二源上线后 | not_assessed（第二源上线后启用） |
| L4 时效 | as_of 落后交易日历的天数 | 计入质量分 |

- **质量分** = 100 × Σ wᵢ·passᵢ（加权通过率）。初始权重：不变式 .30 / 缺口 .25 / 跳变 .20 / 对账 .15 / 时效 .10；**M3（第二源上线前）L3 not_assessed → 对账 .15 权重置 0 后剩余四项归一化**（不变式 .353 / 缺口 .294 / 跳变 .235 / 时效 .118）；判定：<70 隔离、70–85 degraded、≥85 ok。权重与阈值 [OPEN-6]，M3 注入脏数据实测后校准（对应 A4 验收）。
- **隔离语义**：整批进 `quarantine/`（原样保留 + reason.json），不入 silver；告警复用 A0 live-smoke 通道。人工放行后保留原 `first_seen_at` 并记 `released_at`；严格 PIT 查询以 `available_at = max(first_seen_at, released_at)` 为可用时间（RFC-000 §2 隔离放行规则）——隔离期内该数据对系统「不存在」，防止「事后放行」变相前视。
- **已入库数据发现问题**：不 UPDATE——追加更正版本行 + `superseded` 标记表（append-only 侧表），latest 视图自动跳过被标记版本。

### D11 —— 覆盖矩阵：静态能力层 + 运行时层，构造性静默

> 下述 `CoverageReport` 结构（status ∈ available/degraded/missing + as_of/quality_score/reason）为 **RFC-000 §5 全线统一词表**——RFC-003 原 `CoverageEntry`（ok/partial/missing）已废除并对齐至此。

- **静态层**：来自 A1 能力声明的 市场 × 数据类型 × PIT 支持 矩阵（provider 自报，TCK 抽验）。
- **运行时层**：每次分析产出结构化对象，随数据包传编排层、写入报告元数据：

```
CoverageReport{ market, symbol, as_of,
  dims: { ohlcv | fundamentals | fundamentals_period | news | filings | actions :
          { status: available|degraded|missing, as_of, quality_score, reason } } }
```

- 判定：无数据或整批被隔离 → `missing`；质量 degraded 或新鲜度超该数据类型 SLA → `degraded`。
- **消费规则（「缺维度则静默」的机械化）**：`missing` → 该维度分析模块**不执行**，报告记录显式静默项，不产生该维度任何结论——与 B0 构造性溯源同构：无证据行，渲染层无从渲染（接口细节归 RFC-003）；`degraded` → 执行但强制新鲜度/质量横幅。
- KPI 对接（ROADMAP §6）：全量数据率 = 必需维度全 available 的分析占比；缺失率单列上报，**不计入降级成功**。
- A9 的 HK 基本面补全（M3）= 把 HK×fundamentals 从 missing 变 available，源选型 [OPEN-7]。

---

## 6. 决策点汇总

| # | 决策 | 一句话 |
|---|---|---|
| D1 | 双时间 + 观测轴 | 有效时间 / published_at / first_seen_at；全表 append-only |
| D2 | PIT 三级分类 | authoritative / observed / replay；入库判定、永不升级；strict 查询排除 replay |
| D3 | 事实与视图分离 | 只存原始价+公司行动；复权序列永不落库；price_basis 如实标注源口径 |
| D4 | 复权引擎边界 | 纯函数；hfq 回测默认；qfq 必须显式锚；重算对拍喂 A4 |
| D5 | bronze 不可变 | 迁移 = 重放导入；派生层永远可重建；snapshot_ref 行级回溯 |
| D6 | 版本化 reader | schema_version 只加法演进；旧 reader 永久保留；CI v1 fixture 硬门禁 |
| D7 | 表设计 | 六表 + A5 小表；market 作列+分区键不分表 |
| D8 | DuckDB 定位 | 引擎+目录；Parquet 为持久格式；catalog 可重建 |
| D9 | 缓存降级 | silver 为唯一事实库；缓存全删无损正确性 |
| D10 | 质量门 | L1 一票否决；质量分加权；隔离批次 + released_at 语义 |
| D11 | 覆盖矩阵 | 静态+运行时两层；missing 维度构造性静默 |
| D12 | A0 双抓（v3 as-built） | ohlcv_raw 事实层→silver / ohlcv 视图→对账区；US 带 actions，CN/HK 待 M3；schema_version=3 |

## 7. [OPEN] 汇总

> OPEN-1/2/3 已被双评审共识 + as-built 解决，转为决策（下方「已决」行，依据标注）；OPEN-4–7 保留待定。本 RFC 无 [OPEN-BUDGET]/[OPEN-JUDGE]/[OPEN-STORAGE] 类维护者裁决项（cassette 存放归 RFC-003 [OPEN-STORAGE]）。

| # | 问题 | 影响 / 决策 |
|---|---|---|
| OPEN-1（已决） | D12 生效时机与 v1 qfq 快照归类 | **已实施为 v3 as-built**（ADR-0003 + M0 加固）：raw 事实层 + 复权视图双抓落地；v1/v2 存量 qfq 归对账区不进事实表。依据：两评审共识 + 已落地代码 |
| OPEN-2（已决） | A股复权对账 ground truth | **决策：以交易所披露的公司行动 + 公开公式自算为规范**，东财 qfq 仅作回归对拍参考；超容差 → 告警人工裁（**DR+Codex 共识**：东财算法无文档、会漂移，不可作 ground truth）。定 A4「复权正确性」验收 |
| OPEN-3（已决） | 在线分析取数是否 write-through 入 silver | **决策：M3 不做在线 write-through**（DR 支持默认；在线溯源已由 trace blob 闭环）。Codex 的 immutable bronze staging（在线路径先写不可变 bronze 暂存、批量校验后入 silver）记为**「未来若做的既定形态」**，M3 不实施 |
| OPEN-4 | fundamentals_snapshot 用宽列还是 MAP 列（指标集随源演进） | 查询便利性 vs 演进成本 |
| OPEN-5 | news_id 去重键（url 可缺失/变化；标题相似度去重是否入 M3） | 新闻重复率与 C2b 语料质量 |
| OPEN-6 | 质量分权重与隔离阈值（待 M3 脏数据实测校准） | A4 验收数值 |
| OPEN-7 | HK 基本面免费源选型（A9 的 HK 补全用哪个源） | HK×fundamentals 能否 available |

## 8. 实施切分（M0–M3）与验收映射

| 阶段 | 本 RFC 范围内动作 |
|---|---|
| M0（已完成） | D12 已落地为 **v3 as-built**（run-id 目录 + `_COMPLETE` + manifest v3；ADR-0003 + M0 加固提交）——抢日历时间目标达成 |
| M1（契约冻结） | A1 能力声明加 `pit_support`/`rate_limit`/`price_basis`；**与 RFC-000 对齐 `snapshot_ref`（含 market+run-id）/`available_at`/`AnalysisContext` 并随 IR 于 M1 冻结**（DR 5：对齐提前到 M1）；E3 裁决 license_tag 词表（bronze/silver 全程默认本地 `~/.bellwether/`，不入公开渠道） |
| M2 | 无 A3 依赖（C2a cassette 独立）；规范化序列化定序/定精度（RFC-000 §8）保快照哈希稳定 |
| M3 | silver/gold 全量落地；`import-snapshots` 迁入 A0 + C2a；A4 质量门（L3 not_assessed 至第二源上线）；A5 主表/日历；A7a filings+blob；A9 覆盖矩阵 + HK 基本面；C2b 按 `as_known_at` 喂评测的适配器归 RFC-003 |

ROADMAP 验收 → 机制映射：**抽检零前视** = as_known_at 宏（`available_at` 判据，RFC-000 §2）+ pit_class 强制 + append-only（D1/D2）；**复权重算与源一致** = D4 对拍进 A4-L3（同源回归；跨源验收待第二源）；**A0 早期快照完整迁移** = D5 重放导入 + D6 CI fixture + 全量哈希校验报告。

## 9. 风险

- **akshare 接口漂移**（高）：bronze 忠实存原始响应 + reader 版本化——漂移只影响新 reader，不毁历史资产。
- **DuckDB 大版本升级**：Parquet 为持久格式 + catalog 可重建（D8），锁定风险已结构性消除。
- **许可暴露**（联动 E3/风险 #8）：本存储含 Yahoo/东财原始数据，**默认本地私有**；任何导出面（评测 cassette、fixture）必须过 E3 三选一策略；license_tag 行级贯穿。
- **存储增长**：90 标的日频为 KB 级/日，年增 <1GB 量级（A7a blob 单独计，10-K 每份 MB 级）——非风险，不做提前优化。
