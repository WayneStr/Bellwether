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
```

## 模型可配置

角色化（`parse` / `synthesis` / `deep_report`）+ 三级覆盖：`CLI 参数 > config.toml > 内置默认`。代码任何地方都不硬编码模型名，一律经 `ModelRouter` 解析。用第三方中转时，先 `bellwether models` 查它实际支持的模型 id 再填。

## 数据源与网络

| 市场 | 行情 | 基本面 | 新闻 |
|------|------|--------|------|
| 美股 US | yfinance | yfinance | yfinance |
| A股 CN | 东财 `stock_zh_a_hist` → 新浪 `stock_zh_a_daily`（降级链） | `stock_a_indicator_lg` | 东财 `stock_news_em` |
| 港股 HK | 新浪 `stock_hk_daily` | （暂留空，待补） | 东财 `stock_news_em` |

> akshare 数据源在国内直连最稳。代码已对东财域自动绕过系统代理直连（`_bypass_proxy_for_eastmoney`），一般无需手动配代理规则；前提是本机能直连相应站点。

## 可靠性与溯源

- **数据源**：异常按类型决定行为（连接/限流类退避重试，空数据立即失败）；单源熔断（连续失败后快速跳过，冷却自愈）；A股行情东财失败自动降级新浪。
- **LLM**：限流/瞬态退避重试；模型持续不可用时自动降一档（`deep_report`→`synthesis`→`parse`，只换模型 id 不换任务参数），报告尾部**明示**降级；认证失败立即明示不掩盖。用户显式 `--model` 时不降级。
- **溯源**：每次 `analyze` 落一份 provenance trace（输入哈希/prompt 版本/模型链/完整 tool 调用记录）到 `~/.bellwether/traces/`，成功失败都记录；输出与落盘文本统一脱敏（密钥零泄漏，见 `SECURITY.md`）。

## 测试

```fish
uv pip install -e ".[dev]"
pytest        # 112 个单测：确定性计算全覆盖 + 数据源/LLM 两侧故障注入（熔断/重试/降级）
```

## 合规

研究辅助工具，非投顾。所有数字来自工具（不臆造）；每份报告附免责声明、标注数据来源与时间；不给个性化买卖指令，不接实盘下单；呈现推理链、保持人在环内。
