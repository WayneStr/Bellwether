# Bellwether

股票分析与规划 AI Agent。**研究与分析辅助工具，不是投资顾问**——只输出分析与依据，不给个性化买卖指令，不接实盘下单。

完整设计见 [DESIGN.md](DESIGN.md)。

## 能力

- **四大分析模块**：技术面（自实现指标 MA/RSI/MACD/布林带）· 基本面（估值 + 简版 DCF + 同行对比）· 情绪面（新闻/催化剂）· 组合/风险（相关性/波动率/回撤/集中度）
- **多市场**：美股（yfinance）· A股 / 港股（akshare），代码自动路由，上层零改动
- **模型全可配置**：角色化（parse/synthesis/deep_report）+ 三级覆盖，支持自定义 API 地址（中转/代理）
- **底线**：所有数字来自工具（防幻觉）· 每份报告附免责声明 · 确定性计算全覆盖单测

## 设计灵魂

LLM 只做推理综合，**不做算术**——指标/估值/组合统计全部用代码确定性计算，作为「事实」喂给 LLM 解读；一切数据经 tool 获取，绝不凭记忆臆造。

## 安装

需要 Python 3.11+。

```fish
# 用 uv（推荐）
uv venv
source .venv/bin/activate.fish      # bash: source .venv/bin/activate
uv pip install -e .
uv pip install akshare               # 分析 A股/港股时需要（可选）
```

## 配置

1. 复制模板并按需改模型：`cp config.example.toml config.toml`
2. 设置 API Key（不写进配置文件）：`set -x ANTHROPIC_API_KEY sk-...`（bash: `export ...`）；
   或存进系统钥匙串：`uv pip install -e ".[secure]"` 后 `bellwether config set-key`（环境变量始终优先）
3. （可选）自定义 API 地址（中转/代理）：`config.toml` 的 `[api] base_url`，或环境变量 `ANTHROPIC_BASE_URL`（前者优先）

## 用法

```fish
# 单股分析（代码自动识别市场：字母→美股，6位数字→A股，5位数字→港股）
bellwether analyze AAPL                 # 美股
bellwether analyze 600519               # A股（贵州茅台）
bellwether analyze 00700                # 港股（腾讯）
bellwether analyze AAPL --deep          # 深度报告（技术+基本+情绪+同行+情景）
bellwether analyze AAPL -o report.md    # 导出 markdown

# 组合/风险分析（多只，确定性指标，不经 LLM）
bellwether portfolio AAPL MSFT 600519 --period 1y

# 配置 / 模型
bellwether config show                  # 生效模型 + key 状态（含来源）+ API 地址
bellwether config set-key               # 把 key 存入系统钥匙串（需 [secure] 可选依赖）
bellwether models                       # 列出当前 API 地址可用的模型 id
bellwether analyze AAPL --model <id> --temperature 0.2   # 运行时覆盖模型

# 评测（C1）：对结构化报告（report.json）四维判分
bellwether eval ~/.bellwether/traces/<日期>/            # 目录下全部 *-report.json
bellwether eval <trace_id>-report.json --json           # 机器可读 EvalReport
bellwether eval <...> --judge --n-judge 3               # 追加 LLM 推理质量评审（花费额度）

# 门禁（C3）：对评测档案按 eval/gates.yaml 判定（劣化拦截）
bellwether gate candidate.json                          # 零容忍/地板维度
bellwether gate candidate.json --baseline baseline.json # 配对退化判定（bootstrap CI）
```

## 模型可配置

角色化（`parse` / `synthesis` / `deep_report` / `judge`）+ 三级覆盖：`CLI 参数 > config.toml > 内置默认`。代码任何地方都不硬编码模型名，一律经 `ModelRouter` 解析。用第三方中转时，先 `bellwether models` 查它实际支持的模型 id 再填。`judge`（评测评审员）不参与降级链——评审模型漂移会破坏评测可比性，失败即明示。

## 数据源与网络

| 市场 | 行情 | 基本面 | 新闻 |
|------|------|--------|------|
| 美股 US | yfinance | yfinance | yfinance |
| A股 CN | 东财 `stock_zh_a_hist` → 新浪 `stock_zh_a_daily`（降级链） | `stock_a_indicator_lg` | 东财 `stock_news_em` |
| 港股 HK | 新浪 `stock_hk_daily` | （暂留空，待补） | 东财 `stock_news_em` |

> akshare 数据源在国内直连最稳。代码已对东财域自动绕过系统代理直连（`_bypass_proxy_for_eastmoney`），一般无需手动配代理规则；前提是本机能直连相应站点。

## 可靠性与溯源

- **数据源**：异常按类型决定行为（连接/限流类退避重试，空数据立即失败）；单源熔断（连续失败后快速跳过，冷却自愈）；A股行情东财失败自动降级新浪。
- **LLM**：限流/瞬态退避重试；模型持续不可用时自动降一档（`deep_report`→`synthesis`→`parse`，只换模型 id 不换任务参数），报告尾部**明示**降级；认证失败立即明示不掩盖。用户显式 `--model` 时不降级。prompt caching 默认启用（system+tools 缓存断点，多轮 tool-use 大幅省 input token；中转不支持时 `[api] prompt_caching = false` 关闭），cache 读写 tokens 在成本摘要单列如实计价。
- **溯源**：每次 `analyze` 落一份 provenance trace（输入哈希/prompt 版本/模型链/完整 tool 调用记录）到 `~/.bellwether/traces/`，成功失败都记录；输出与落盘文本统一脱敏（密钥零泄漏，见 `SECURITY.md`）。
- **评测**：`bellwether eval` 对 report.json 四维判分——事实性（R1 裸数字重扫 + R7/R8 溯源逐位重算，程序化硬判）、完整性（分档机械清单）、合规（措辞规则层）、推理质量（`--judge` 启用 LLM 评审，n≥2 出 95% 置信区间）。程序化维度重复评分逐位一致；确凿违规退出码 1。
- **门禁**：`bellwether gate` 按 `eval/gates.yaml` 声明式配置判定——事实性/合规零容忍（任何一例违规即红，无豁免）、完整性分数地板、推理/综合与 baseline-of-record 逐例配对（case 聚类 bootstrap 单侧置信区间，同种子逐位可复现）；与基线的版本指纹（模型/prompt/judge）失配时拒绝比较并要求重定基线。

## 测试

```fish
uv pip install -e ".[dev]"
pytest        # 248 个单测：确定性计算全覆盖 + 数据源/LLM 两侧故障注入 + IR 构造性核验 + 评测器守门员
```

## 合规

研究辅助工具，非投顾。所有数字来自工具（不臆造）；每份报告附免责声明、标注数据来源与时间；不给个性化买卖指令，不接实盘下单；呈现推理链、保持人在环内。

## 许可

本项目代码以 [Apache-2.0](LICENSE) 发布。

**代码许可不等于数据许可**：通过 yfinance / akshare 取得的行情、基本面与新闻数据，其权利归上游数据源所有，受各自条款约束——本仓库不再分发任何原始数据（快照与 cassette 均为本地私有，不入 git）。逐源审计结论见 [docs/data-licensing.md](docs/data-licensing.md)。
