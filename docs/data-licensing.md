# 数据许可审计（E3）

> 状态：**Accepted**（M1 交付物）· 审计日 2026-07-17 · 采纳记录 ADR-0005 · 审计人：维护者 + AI 协作
> **性质声明：本文为工程尽调口径的许可审计，非法律意见；重大决策（商业化、公开数据资产、收到权利主张时）建议人工法务复核。**
> 所有条款结论基于审计日当天查证的真实来源（附录 A 给出 URL 与引文出处）；条款可能随时变更，任何源条款变更或收到权利方通知时须重开审计并录 ADR。

## 1. 概述

- 用法事实（审计对象）：A0 每日快照 90 标的行情/基本面/新闻**原始响应**存本地 `~/.bellwether/snapshots/`（不入 git）；C2a cassette 同为本地私有；公开仓库只放代码与 manifest 哈希；公开报告只引用派生数字与短摘录。数据链：yfinance（Yahoo）、akshare（东财 push2his 行情/东财新闻/legulegu 估值/新浪 A股港股行情）。
- 核心判断框架：**「采集」「私有留存」「公开再分发」是三个独立风险轴**。所有被审计源的条款都以「向第三方提供/散发」为再分发要件；本项目现阶段无任何分发行为，主要剩余风险在采集方式（自动化访问）与未来公开产物的边界控制。
- 三个书面结论见 §3；C2a 存放策略与 ADR-0004 云备份开关由本文裁决。

## 2. 逐源审计

| # | 源 | 库许可 | 采集风险 | 私存风险 | 公开原文 | license_tag |
|---|---|---|---|---|---|---|
| 1 | Yahoo（经 yfinance） | Apache-2.0 | 中 | 低 | **禁止（高）** | private-ok-backup-ok |
| 2 | akshare（库本身） | MIT | —（随上游） | — | — | —（按上游逐源标） |
| 3 | 东方财富 | — | 中 | 低 | **禁止（高，涉交易所权利）** | private-ok-backup-ok |
| 4 | 新浪财经 | — | 中（条款明文提「抓取」） | 低 | **禁止（高）** | private-ok-backup-ok |
| 5 | legulegu | — | 低-中（无明文条款） | 低 | 避免（中-高） | private-ok-backup-ok |
| 6 | GICS | 专有（MSCI/S&P） | — | — | **不使用即无风险；使用=高** | 不引入 |
| 7 | A6 专业源 | 商业合同 | 低（有授权） | 按合同 | 按合同（个人档普遍禁止） | plugin-user-licensed |

### 2.1 yfinance / Yahoo Finance
- **库许可**：Apache Software License（README 明示，LICENSE.txt 在发行包内）。
- **上游条款要点**（Yahoo ToS，§ 号为原文条号）：§2(e) 未明示允许不得为商业目的访问/复用服务；§2(h) 不得复制/分发/传播/建立衍生作品/为商业目的利用服务内容；§2(d)(ix) 未经事先明示许可不得以自动化手段（robots/spiders/scrapers/数据挖掘工具）采集数据；§2(d)(x) 不得用服务内容建立数据库/存档/数据源（data feed）。yfinance README 官方免责表述："yfinance is **not** affiliated, endorsed, or vetted by Yahoo, Inc."、"intended for research and educational purposes"、"the Yahoo! finance API is intended for **personal use only**"，并指引用户自行查阅 Yahoo 条款。
- **我们用法定性**：个人研究、非商业、低频快照、纯本地留存——与 README「personal use / research」定位一致；但自动化采集本身处于 §2(d)(ix) 的灰区（业界普遍容忍：yfinance 公开维护多年、PyPI 正常分发；无对个人研究用途的公开执法记录，此点为观察非保证）。
- **再分发边界**：原始数据（含批量表格、可下载序列、数据 feed）绝不公开；§2(d)(x) 意味着「用 Yahoo 数据建公开数据库」明确违规。
- **约束建议**：保持限流与缓存（减少请求量）；公开面仅派生指标；文档复述 README 免责三句。

### 2.2 akshare（库本身）
- **库许可**：MIT（akfamily/akshare LICENSE）。
- **性质说明**（官方文档）：「AKShare 开源财经数据接口库所采集的数据皆来自公开的数据源，不涉及任何个人隐私数据和非公开数据。本项目提供的数据接口及相关数据仅用于**学术研究**，任何个人、机构及团体使用本项目的数据接口及相关数据请**注意商业风险**。」即：库是抓取公开网页/接口的客户端，不代理任何上游授权，上游合规责任在使用者。
- **定性与约束**：库许可干净；上游风险按 §2.3–2.5 逐源承担。我们的研究用途与其声明口径一致。

### 2.3 东方财富（push2his 行情 / 新闻）
- **条款要点**（《法律声明》，条号为原文）：第八条——未经东方财富或权利人**事先书面许可**，不得复制/修改/转载/传播/经销/翻印/出版或任何其它形式散发网站内容；**第十条——未经深交所、上交所等交易所事先书面同意，不得以任何方式将本网站的行情信息复制、传播、转播、演示或散发**（行情数据的权利链上溯至交易所）；第四条——不得干扰网站正常运作、不得施加不合理或不成比例的高负载。push2his 为未公开文档的站内接口，无单独 API 条款可查（查无明确 API 条款本身即本次结论之一）。
- **定性**：私有快照非「散发」；采集为灰区，第四条把负载列为红线——M0 已实测东财限流并做了突发限速缓解与新浪回退（见 git log），须保持。
- **再分发边界**：行情原文公开=同时触碰东财与交易所两层权利，为**全链路最严红线**；新闻正文全文转载亦违第八条，报告限短摘录+署名来源。

### 2.4 新浪财经（A股/港股行情回退）
- **条款要点**：《新浪通行证服务协议》8.4——「未经新浪许可，任何单位和个人不得以任何形式全部或部分地复制、转载、链接、**抓取**、反向工程等使用新浪享有知识产权的内容」（被审计源中唯一明文写「抓取」者）；财经频道页脚免责——「行情数据以及其他资料均来自**合作方**，仅作为用户获取信息之目的，并不构成投资建议」（新浪自身亦是行情的被授权转发方）。
- **定性与约束**：仅作回退源、低频、私存；hq.sinajs.cn 为非公开文档接口（需 Referer 头），同灰区管理；公开边界同东财。

### 2.5 legulegu（乐咕乐股，估值百分位）
- **条款要点**：全站可查到的仅有《关于我们》页免责声明：「图表所示结果或标示仅供学习参考使用，均不构成交易依据……本站概不负责」「《数据》栏目将完全免费展示」。**未查到任何针对数据再利用、再分发或抓取的服务条款**——「查无明确条款」是本次的如实结论；无条款≠授权，按其他源同等标准处理。
- **定性与约束**：其估值数据本身是 legulegu 对公开行情的派生汇编（汇编者权益属它）；小站，礼貌限流比大站更重要（可用性与道义双重原因）；私存允许，公开面避免批量转录其序列，报告引用单点数值须署名。

### 2.6 GICS 与替代方案
- **GICS**：MSCI 与 S&P 的**独占财产与注册商标**，官方口径为任何使用/访问其产品需取得许可（GICS Direct 为付费授权产品）。**确认：不可免费再分发，开源项目不得内置 GICS 代码/名称/映射表；亦不得用「GICS」字样描述自建分类（商标）。**
- **替代方案 a：yfinance sector/industry 字段**——Yahoo 页面使用 11 部门词表（Technology、Financial Services、Consumer Cyclical、Consumer Defensive 等），**与 GICS 官方词表命名不同**（GICS 为 Consumer Discretionary/Staples、Financials、Information Technology），非 GICS 标签，无 GICS 许可问题；数据本身受 Yahoo ToS 约束，边界同 §2.1（报告中逐票标注 sector 属少量事实陈述，低风险）。
- **替代方案 b：申万/中证公开层级**——申万 2021 版行业分类标准由申万宏源公开发布（官网与公开研报），中证行业分类标准说明以公开 PDF 发布于中证指数官网。**使用层级名称并标注个股归属（署名出处）属公开标准的事实性引用，低风险**；但**完整成分股映射表**是汇编成果，不放公开仓库，逐票标注即可。A5 文档须明示「非官方 GICS」。

### 2.7 免费源天花板与 A6 专业源许可模式（M5 铺垫）
免费源的共同天花板：无授权合同（用法靠灰区容忍）、无 SLA、条款可单方变更、原文永不可公开。专业源的许可模式（均为审计日官网条款）：**Polygon** 个人档限「personal, non-business use」，且明文禁止向第三方再分发市场数据**及其衍生作品**（比免费源更严），商业用途须转 Business 条款；**FMP** 禁止转售/转授权/分发数据及**源自数据的派生数据**，公开展示需另签 Data Display and Licensing Agreement；**Tushare Pro** 服务协议第二条(二)5「仅可为非商业目的使用，并仅可用作**个人查看使用**」、(二)6 禁止账号转让/出租/分享。共同含义：专业源数据在任何档位都**不得进入项目共享资产**（cassette/黄金集/公开报告原文）；「专业源档」公开基准只能发布评测分数与结论（分数是对 agent 行为的度量，非市场数据派生物），此口径 M5 开工前需按届时条款复核。

## 3. 三个书面结论（工程尽调口径，非法律意见）

### 结论 A：本地私有快照/cassette 不构成再分发 —— **是（不构成）**
推理：(1) 被审计条款中的禁止对象「散发/传播/转载/redistribute/distribute/transmit」均以**向第三方提供**为要件（Yahoo §2(h)、东财第八/十条、新浪 8.4 的行为动词全部指向对外提供）；本地落盘自始至终无第三方接收，行为不该当。(2) 存储目录不入 git，公开仓库仅含 manifest 哈希（SHA-256 不可逆，不构成内容提供）。(3) 用途上与各源自我声明一致：Yahoo「personal use」、akshare「学术研究」；对 CN 源另有《著作权法》(2020 修正) 第二十四条第(一)项「为个人学习、研究或者欣赏，使用他人已经发表的作品」的合理使用支撑（受三步检验约束，个人研究快照不影响作品正常使用）。
**边界澄清**：本结论只解决「再分发」轴；**采集行为的 ToS 灰区风险独立存在**（Yahoo §2(d)(ix)、新浪 8.4「抓取」、东财第四条负载），以限流、低频、个人研究姿态管理，且 D2/D4 的限流器为持续义务而非一次性措施。

### 结论 B：加密云备份不改变结论 A —— **允许，附四条件**（ADR-0004 云备份开关：**开**）
推理：向自有云存储上传是**委托保管**而非向公众或第三方「提供」；客户端加密下云服务商连内容都不可见，披露为零；与本地磁盘在分发语义上无差别。条件：
1. **客户端加密**（上传前加密，密钥仅本人持有；服务商无明文可见）；
2. **仅本人可访问**（私有桶/私有网盘目录，本人凭证，无共享链接、无团队空间）；
3. **仅作灾备**：不得经备份渠道向任何第三方提供访问；**未来若出现多维护者共享备份，即构成向第三方提供，须重开本审计**；
4. 遵守所选云服务商自身 ToS（合法个人数据的加密备份为通常允许用途）。

### 结论 C：公开仓库与公开报告红线清单
**绝不进入公开仓库**：① A0 快照原始响应文件的任何片段；② C2a cassette 原文（**含加密压缩形态**——密文入库等于把分发风险压在单一密钥上，不做）；③ 新闻正文全文或长段转载；④ GICS 代码/名称/映射表；⑤ 任何源的批量原始数据表（整段 OHLCV 历史、完整成分股映射、legulegu 序列转储）；⑥ A6 专业源数据的任何形式。
**允许进入公开仓库**：代码、recorder/采集脚本、manifest（哈希+元数据：时间戳/来源 ID/license_tag/字节数，**不含数据值**）、合成数据的 schema 样例。
**公开报告（含 F5 样例画廊）安全边界**：① 派生数字（自算指标、DCF 结果、涨跌幅、比率）+ 注明来源与 as_of——事实与自有分析，安全主体；② 个别原始数据点（如某日收盘价）作为事实陈述：少量、分散、**不成表、不成可机读序列**；③ 新闻限标题级或一两句短摘录 + 来源署名（+链接），置于评论/分析语境；④ 行业分类用 Yahoo 词表或申万/中证层级名并标注「非官方 GICS」，不使用 GICS 字样自述；⑤ 报告不附任何可下载/可解析的原始数据附件。

## 4. license_tag 词表建议（供 RFC-000 §9 采纳）

| tag | 语义 | 当前适用 |
|---|---|---|
| `public-ok` | 原文可公开再分发 | 预留（EDGAR/US 政府作品，A7a 定标时复核） |
| `private-ok-backup-ok` | 原文限私有；本地 + 满足结论 B 四条件的加密个人云备份均可；公开面仅结论 C 边界 | **Yahoo/东财/新浪/legulegu 全部快照与 cassette** |
| `private-only` | 原文限本地磁盘，禁云备份 | 预留（本次未发现需要此级的源） |
| `derived-only` | 原始响应不长期留存，资产仅保留派生指标 + 哈希 | C2a「仅派生」后备选项 |
| `plugin-user-licensed` | 许可属最终用户；数据禁止进入任何项目共享资产；基准仅发布评测分数 | A6 专业源（Polygon/FMP/Tushare Pro） |

- RFC-000 §1 占位标签 `private-do-not-redistribute (pending E3 audit)` 自本文接受起替换为上表；存量 manifest 迁移按 §1 词表批量改写。
- **C2a 三选一裁决**：「私有存储」（= `private-ok-backup-ok`）成立且为默认；「本地再生成」「仅派生」降为后备。

## 5. README 提示文案草稿

> **Data sources & licensing.** Bellwether's free tier fetches data from public sources (Yahoo Finance via `yfinance`; Eastmoney, Sina Finance and legulegu via `akshare`). This data is for **personal research use only**. Bellwether stores raw responses **locally on your machine** (`~/.bellwether/`) and never redistributes them; the public repository contains only code and content hashes. You are responsible for complying with each upstream source's terms of service. Bellwether is not affiliated with, endorsed, or vetted by any data source. Nothing produced by Bellwether is investment advice.
>
> 中文提示：免费档数据来自公开数据源，仅供个人研究；原始数据只存本地、绝不再分发；使用者须自行遵守各数据源条款；本项目输出不构成投资建议。

## 6. 插件许可责任移交声明草稿（A6/TCK 文档用）

> **Professional data plugins: license pass-through.** When you use a professional data source plugin (e.g. Polygon, FMP, Tushare Pro), the data license is a contract **between you and the data vendor**. Bellwether does not sublicense, proxy, or extend that license. You must (1) hold your own valid API credentials, (2) verify that your plan tier permits your usage (personal vs. commercial; storage/caching; derived-works clauses), and (3) keep vendor data out of any shared or published artifact — including evaluation cassettes, golden sets, and public reports. Bellwether's evaluation pipeline will refuse to record `plugin-user-licensed` data into shareable assets. Violations of vendor terms are your responsibility.

## 附录 A：引用来源清单（全部查证于 2026-07-17）

| # | 内容 | URL |
|---|---|---|
| 1 | yfinance README（Apache 许可、三句免责） | https://github.com/ranaroussi/yfinance/blob/main/README.md |
| 2 | Yahoo Terms of Service §2(d)(ix)/(x)、§2(e)、§2(h) | https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html （guce.yahoo.com/terms 重定向至此） |
| 3 | akshare LICENSE（MIT） | https://github.com/akfamily/akshare/blob/main/LICENSE |
| 4 | akshare 数据来源与声明（公开数据源/学术研究/商业风险） | https://github.com/akfamily/akshare 及 /blob/main/docs/introduction.md |
| 5 | 东方财富《法律声明》第四/八/十条 | https://about.eastmoney.com/home/disclaimer |
| 6 | 东方财富《服务协议》 | https://about.eastmoney.com/home/protocol |
| 7 | 新浪财经页脚免责声明（行情来自合作方） | https://finance.sina.com.cn/stock/ |
| 8 | 新浪通行证服务协议 8.4（复制/转载/链接/抓取/反向工程） | https://passport.sinaimg.cn/html/sso/signupagreement_x.html |
| 9 | legulegu《关于我们》（免责声明；无数据再利用条款） | https://legulegu.com/about |
| 10 | MSCI GICS 官方页（专有与许可要求） | https://www.msci.com/indexes/index-resources/gics |
| 11 | S&P DJI GICS 页（商标与授权产品） | https://www.spglobal.com/spdji/en/landing/topic/gics/ |
| 12 | Yahoo sector 词表（11 部门，非 GICS 命名） | https://finance.yahoo.com/sectors/ |
| 13 | 申万 2021 版行业分类标准说明（公开发布） | https://wxweb.swsresearch.com/swsreport/2021_08/328340.pdf |
| 14 | 中证行业分类标准说明（公开 PDF） | https://oss-ch.csindex.com.cn/industryClassification/中证行业分类的说明.pdf |
| 15 | Polygon for Individuals ToS（个人非商业、禁再分发含衍生作品） | https://polygon.io/legal/individuals-terms-of-service |
| 16 | Polygon Market Data ToS | https://polygon.io/legal/market-data-terms-of-service |
| 17 | FMP Terms of Service（禁转售/分发派生数据；展示需另签协议） | https://site.financialmodelingprep.com/terms-of-service |
| 18 | Tushare 数据服务协议 第二条(二)5/6 | https://tushare.pro/document/1?doc_id=405 |
| 19 | 《著作权法》(2020 修正) 第二十四条第(一)项 | https://www.ncac.gov.cn/xxfb/flfg/flfg_532/202103/t20210309_50530.html |

*条款为动态对象：任一 URL 内容变更、任一源发出权利主张、或项目用法超出本文「用法事实」范围（特别是商业化与多维护者共享）时，本审计失效，须重开并录 ADR。*
