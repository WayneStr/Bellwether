# Bellwether 设计文档

> 一个用于股票分析与规划的 AI Agent。名字取「领头羊 / 风向标」之意。
> 状态：**四大模块已实现并验证**（技术/基本/情绪/组合 · 多市场 US/CN/HK）· 47 单测全绿 · 最后更新 2026-07-15
> 本文档是设计蓝图；实际数据结构以 `models.py` 为准，实现与设计的差异见 §11 末「实现说明」。

---

## 0. 定位与边界（先读这一节）

**Bellwether 是研究与分析的辅助工具，不是投顾。**

- ✅ 它做的：整理数据、多角度分析、跨因子综合、情景推演、生成带依据的研究报告。
- ❌ 它不做的：生成个性化买卖指令、替用户下决策、接入实盘下单、承诺收益。
- 每次输出附**免责声明**（非投资建议，据此操作风险自负），标注**数据时效与来源**，保持**人在环内**（呈现推理链，用户自行判断）。

这条边界贯穿全部设计，任何模块不得越界。

---

## 1. 设计原则（三条灵魂）

这三点决定这个 agent 可不可信，优先级高于任何技术选型。

### 1.1 LLM 只做推理，不做算术
所有数字——RSI、MACD、PE、PB、ROE、DCF、波动率、回撤——一律由**成熟库确定性计算**，作为「事实」喂给 LLM。LLM 负责*解读、跨模块综合、写情景、生成报告*。**绝不让 LLM 自己算指标或编造财务数据**。这是防幻觉的地基，也是每个确定性计算都要有单元测试的原因。

### 1.2 一切数据经由 Tool 获取，绝不凭记忆
每个数据源与计算封装成 agent 可调用的 `tool`。LLM 要用某个数字，必须调用对应 tool 拿实时数据，而非从训练记忆里取。天然契合 CLI+LLM，也为日后转 MCP server 铺路。

### 1.3 结构化输出贯穿始终
每个分析模块输出 Pydantic 结构体而非自由文本。好处：便于 LLM 综合、便于写测试、便于日后接 Web 前端。自由文本只在最终报告层出现。

---

## 2. 架构总览

```
┌─────────────────────────────────────────────────────────────┐
│  CLI 交互层    typer + rich                                   │
│    bellwether analyze AAPL --deep --model <...>              │
├─────────────────────────────────────────────────────────────┤
│  Agent 编排层  Claude tool-use loop                          │
│    · 规划：拆解「分析某股」→ 该调哪些模块/tool               │
│    · 综合：各模块结构化结果 → 多因子研判 + 情景推演          │
│    · 输出：结构化报告 + 免责声明                             │
│    ▲ 模型由 ModelRouter 按角色分配（见 §3）                  │
├──────────────┬──────────────┬──────────────┬─────────────────┤
│  分析层（模块化：每个 = 一组 tool + 确定性计算）            │
│  基本面       技术面         情绪面         组合/风险         │
│  估值·财报    指标·形态      新闻·舆情      仓位·回撤·再平衡  │
├──────────────┴──────────────┴──────────────┴─────────────────┤
│  数据层  MarketDataProvider 抽象接口（可插拔）+ 缓存         │
│    ┌──────────┬──────────┬──────────┐                       │
│    │ 美股(P1)  │ A股       │ 港股      │  ← 新增市场只写子类  │
│    │ yfinance │ akshare  │ akshare  │                       │
│    └──────────┴──────────┴──────────┘                       │
└─────────────────────────────────────────────────────────────┘
```

「多市场」的可插拔性完全落在数据层的**一个抽象接口**上（§4）；上层模块对市场无感知。**P1 只实现美股 Provider**，但接口一次性设计到位。

---

## 3. 模型配置层（★ 核心特性：模型全可配置）

需求：**深度报告用哪个模型由用户自定义**。设计上做成一等公民——不是某个开关，而是整套「角色化模型 + 三级覆盖」机制。代码中**任何地方都不硬编码模型名**，一律经 `ModelRouter` 解析。

### 3.1 角色化（按任务分配模型，省钱又快）

| 角色 role | 用途 | 默认模型 |
|-----------|------|----------|
| `parse` | 财报字段抽取、新闻解析等轻量结构化 | `claude-haiku-4-5-20251001` |
| `synthesis` | 跨模块综合研判（agent 编排主循环） | `claude-sonnet-5` |
| `deep_report` | 深度报告（用户最关心、最可能自定义的一档） | `claude-opus-4-8` |

> 可选模型 id：`claude-opus-4-8`（Opus 4.8）、`claude-sonnet-5`（Sonnet 5）、`claude-haiku-4-5-20251001`（Haiku 4.5）、`claude-fable-5`（Fable 5）。

### 3.2 三级覆盖优先级

```
CLI 运行时参数   >   config.toml 配置   >   代码内置默认
--model / --temperature      [models] 段          ModelConfig 默认值
```

任一角色都能被覆盖；深度报告额外在 CLI 暴露 `--model` / `--temperature` / `--max-tokens`，做到「专门的选项接口给用户自定义」。

### 3.3 配置形态（config.toml）

```toml
[models]
parse       = "claude-haiku-4-5-20251001"
synthesis   = "claude-sonnet-5"
deep_report = "claude-opus-4-8"

# 每个角色可单独调参；deep_report 通常最需要精调
[models.params.deep_report]
temperature = 0.3
max_tokens  = 8000
# thinking = true   # 若启用扩展思考，实现时按 SDK 能力接入

[models.params.synthesis]
temperature = 0.2
max_tokens  = 4000
```

### 3.4 代码骨架

```python
class ModelParams(BaseModel):
    temperature: float = 0.3
    max_tokens: int = 4096
    extra: dict[str, Any] = {}          # 透传 SDK 其他参数，向前兼容

class ModelSpec(BaseModel):
    model: str                          # 模型 id 字符串，不写死
    params: ModelParams

class ModelConfig(BaseModel):           # 从 config.toml 加载
    parse: ModelSpec
    synthesis: ModelSpec
    deep_report: ModelSpec

class ModelRouter:
    """唯一的模型解析入口。任何调用 LLM 的地方都必须走这里。"""
    def resolve(self, role: str, *,
                model: str | None = None,      # CLI 覆盖
                **param_overrides) -> ModelSpec:
        spec = getattr(self.config, role)
        if model:            spec = spec.copy(update={"model": model})
        if param_overrides:  spec.params = spec.params.copy(update=param_overrides)
        return spec
```

> **扩展点（P1 不实现，留接口）**：`model` 是自由字符串 + 一层 `LLMProvider` 抽象，未来若要接非 Claude 模型，只需新增 provider 实现，`ModelRouter` 契约不变。

---

## 4. 数据层设计（可插拔的关键）

### 4.1 抽象接口

```python
class MarketDataProvider(ABC):
    market: str                                   # "US" / "CN" / "HK"

    @abstractmethod
    def get_ohlcv(self, symbol: str, start: date, end: date,
                  interval: str = "1d") -> pd.DataFrame: ...   # K线：OHLCV
    @abstractmethod
    def get_fundamentals(self, symbol: str) -> FundamentalData: ...
    @abstractmethod
    def get_news(self, symbol: str, limit: int = 20) -> list[NewsItem]: ...
    @abstractmethod
    def trading_rules(self) -> TradingRules: ...  # 时区/交易时段/涨跌幅限制等差异全封在这
    @abstractmethod
    def resolve_symbol(self, query: str) -> str: ...          # 名称/代码归一化
```

### 4.2 Registry / 工厂（市场路由）

```python
class ProviderRegistry:
    _providers: dict[str, type[MarketDataProvider]] = {}

    @classmethod
    def register(cls, market: str, provider: type[MarketDataProvider]): ...
    @classmethod
    def for_market(cls, market: str) -> MarketDataProvider: ...
    @classmethod
    def for_symbol(cls, symbol: str) -> MarketDataProvider: ...  # 自动识别市场
```

新增市场 = 写一个子类 + `register`，**上层零改动**。

### 4.3 P1 实现：YFinanceProvider（美股）
- OHLCV / fundamentals / news 全部来自 `yfinance`，免费起步。
- `trading_rules()`：美东时区、无涨跌停、T+0 结算语义。
- 财报字段（营收、净利、EPS、现金流等）→ 映射到统一 `FundamentalData`。

### 4.4 缓存
- `requests-cache` 或简单文件缓存（parquet/json），按 `(symbol, 数据类型, 日期)` 键控。
- 每条数据记录 `fetched_at`，报告中标注时效。
- 行情类短 TTL，财报类长 TTL。

---

## 5. 分析层设计（四大模块）

统一模式：**确定性计算产出数字 → 结构化结果 → 交 LLM 解读**。P1 只做基本面 + 技术面（见路线图）。

| 模块 | 确定性计算（库） | LLM 职责 | 输出结构体 |
|------|-----------------|----------|-----------|
| 基本面 `fundamental` | 财报字段、PE/PB/ROE/毛利率、DCF、同行对比 | 财务健康度、成长性、护城河叙事、估值合理性 | `FundamentalReport` |
| 技术面 `technical` | pandas-ta：MA/EMA/MACD/RSI/BOLL、量价、形态 | 趋势/背离/关键位、多周期共振解读 | `TechnicalReport` |
| 情绪面 `sentiment` | 抓新闻/公告，可选情感打分 | 催化剂与风险事件、预期差判断 | `SentimentReport` |
| 组合/风险 `portfolio` | 相关性、波动率、最大回撤、权重、再平衡 | 风险敞口解释、配置逻辑、情景 | `PortfolioReport` |

每个模块对外暴露：
```python
class AnalysisModule(Protocol):
    def compute(self, symbol: str, provider: MarketDataProvider) -> BaseReport:
        """纯确定性：只算数字 + 组装结构体，不调用 LLM。"""
    def as_tools(self) -> list[ToolSpec]:
        """把本模块能力注册为 agent 可调用的 tool。"""
```

---

## 6. Agent 编排层

### 6.1 主循环（Claude tool-use loop）
1. 用户请求 → 系统提示 + 用户意图 交给 `synthesis` 角色模型。
2. 模型规划要调哪些 tool（取数 / 算指标）。
3. 运行 tool，把**结构化结果**回灌。
4. 循环直到模型收集够事实。
5. 模型产出综合研判；若 `--deep`，切 `deep_report` 角色模型生成深度报告。
6. 报告层（rich）渲染 + 附免责声明。

> 实现基于 Anthropic Python SDK 的 tool-use 循环；具体 API（工具调用、扩展思考、prompt caching）在编码阶段以官方 SDK 文档为准。**不引入 LangGraph**——CLI 原型一个 agent loop 足够，待多 agent/复杂分支/持久化工作流出现再评估。

### 6.2 Tool schema 示例

```json
{
  "name": "get_technical_indicators",
  "description": "计算给定美股标的的技术指标（MA/MACD/RSI/BOLL 等）。返回结构化数值，不含结论。",
  "input_schema": {
    "type": "object",
    "properties": {
      "symbol":     {"type": "string", "description": "股票代码，如 AAPL"},
      "indicators": {"type": "array", "items": {"type": "string"}},
      "period":     {"type": "string", "description": "如 6mo / 1y"}
    },
    "required": ["symbol"]
  }
}
```

### 6.3 系统提示要点
- 明确「只做分析，不给买卖指令」的边界。
- 强制「数字必须来自 tool，禁止自行计算或臆造」。
- 要求输出结构：结论 / 依据 / 反面风险 / 数据时效 / 免责声明。
- 呈现推理链，保持人在环内。

---

## 7. 配置系统

`config.toml`（项目根）+ 环境变量（密钥）：

```toml
[models]           # 见 §3.3

[api]              # 可选：自定义 API 地址（代理/中转/兼容网关）
base_url = "https://your-proxy.example.com"

[data]
default_market = "US"
cache_ttl_days = 1

[report]
disclaimer = true
language   = "zh"      # 报告语言
```

- `ANTHROPIC_API_KEY` 等密钥走**环境变量**，绝不写进 config/代码/仓库。
- `[api].base_url` 可自定义 Anthropic 请求地址（也支持环境变量 `ANTHROPIC_BASE_URL`，config 优先）；留空用官方默认。
- 提供 `config.example.toml`，真实 `config.toml` 入 `.gitignore`。

---

## 8. CLI 设计（typer）

```
bellwether analyze <SYMBOL> [--deep] [--model <id>] [--temperature <f>]
                            [--modules fundamental,technical] [--period 1y]
bellwether compare <SYM1> <SYM2> ...          # 横向对比
bellwether portfolio <holdings.csv>           # 组合/风险（P4）
bellwether config show                        # 查看当前生效模型/配置
bellwether models                             # 列出 API 地址下可用模型 id
```

`--model` / `--temperature` 即「深度报告模型的用户自定义接口」，运行时覆盖 config。

---

## 9. 项目结构

```
bellwether/
├── cli.py                  # typer 入口
├── config.py               # 加载 config.toml + 环境变量 → 强类型对象
├── agent/
│   ├── orchestrator.py     # tool-use loop：规划→调模块→综合
│   ├── router.py           # ModelRouter（§3）
│   ├── tools.py            # 各模块能力 → tool schema 注册
│   └── prompts.py          # 系统提示（含合规边界、输出规范）
├── data/
│   ├── base.py             # MarketDataProvider 抽象 + ProviderRegistry
│   ├── yfinance_provider.py# P1：美股
│   └── cache.py
├── analysis/
│   ├── fundamental.py      # P1
│   ├── technical.py        # P1
│   ├── sentiment.py        # P3
│   └── portfolio.py        # P4
├── models.py               # 所有 Pydantic 结构体（含 ModelConfig）
├── report.py               # rich 渲染 + 免责声明
├── config.example.toml
└── tests/                  # 每个确定性计算都有单测
```

---

## 10. 核心数据模型（Pydantic 汇总）

```python
# 数据层
class FundamentalData(BaseModel):   # 原始财务数据（未加工）
    symbol: str; currency: str
    revenue: float | None; net_income: float | None; eps: float | None
    pe: float | None; pb: float | None; roe: float | None
    fetched_at: datetime; source: str

class NewsItem(BaseModel):
    title: str; url: str; published_at: datetime; summary: str | None

class TradingRules(BaseModel):
    market: str; timezone: str; has_price_limit: bool; settlement: str

# 分析层（compute 产出，喂给 LLM）
class TechnicalReport(BaseModel):
    symbol: str
    indicators: dict[str, float]        # {"RSI14": 62.3, "MACD": ...}
    signals: list[str]                  # 确定性规则识别的中性描述，非买卖指令
    fetched_at: datetime

class FundamentalReport(BaseModel):
    symbol: str
    valuation: dict[str, float]         # {"PE": ..., "DCF_fair_value": ...}
    peers_compare: dict[str, Any]
    fetched_at: datetime

# 最终输出
class AnalysisResult(BaseModel):
    symbol: str
    verdict: str                        # LLM 综合研判（叙事）
    evidence: list[str]                 # 依据（引用各模块数字）
    risks: list[str]                    # 反面风险
    data_asof: datetime
    disclaimer: str
```

---

## 11. MVP 分期路线图（每期可独立验证）

**P1 = 美股 · 基本面 + 技术面闭环。** 先做透，再横向纵向扩展。

| 阶段 | 范围 | 验证标准（Definition of Done） |
|------|------|-------------------------------|
| **P0 骨架** | 项目结构、config、ModelRouter、数据抽象接口 + YFinanceProvider、最小 agent loop | `bellwether analyze AAPL` 跑通：agent 调一个 tool 拿到真实 K 线并打印；`config show` 显示生效模型 |
| **P1 美股深度** ⭐ | 基本面 + 技术面模块（确定性计算 + 结构化）、综合研判、`--deep` 报告、模型三级覆盖 | 对一只美股产出含真实估值+技术指标+LLM 综合的报告；所有确定性计算有单测；`--model` 能覆盖深度报告模型 |
| **P2 扩市场** | AksharProvider（A股/港股） | 同一 `analyze` 换 symbol 即分析他市场，上层零改动 |
| **P3 情绪面** | sentiment 模块（新闻/公告） | 报告出现真实近期催化剂/风险事件 |
| **P4 组合/风险** | portfolio 模块 + `portfolio` 命令 | 输入持仓 CSV，产出相关性/回撤/集中度分析与再平衡逻辑 |

### 实现状态（2026-07-15）：P0–P4 全部完成 ✅

四大模块（技术/基本/情绪/组合）+ 多市场（US/CN/HK）均已实现并真实验证，47 单测全绿。与上表设计的**关键偏离**：

- **技术指标自实现**（`analysis/indicators.py`），不用 pandas-ta——后者在 numpy≥2.0 下 import 崩溃且是黑盒；自实现几十行、可逐个单测。
- **港股行情用新浪源** `stock_hk_daily`（东财港股子域 `33.push2his` 部分网络不可达）；A股仍东财。代码对东财域自动绕过系统代理直连（`_bypass_proxy_for_eastmoney`）。
- **港股基本面暂为最小版**（估值留空），akshare 港股估值接口未统一。
- **基本面比率**（ROE/毛利率）在输出层转百分比并标注单位，避免 LLM 误读。
- **`--deep`** 有专门深度 prompt（强制同行对比 + 更长周期 + 乐观/中性/悲观情景）。
- **`portfolio`** 目前是确定性指标 + rich 展示（不经 LLM）；LLM 解读与再平衡建议列为后续增强。
- 实际数据结构以 `models.py` 为准（本文 §10 是早期草图）。

---

## 12. 合规与风控清单

- [x] 每份报告附免责声明（非投资建议）。
- [x] 标注数据来源与 `data_asof` 时效；行情延迟明示。
- [x] 系统提示与输出层双重约束：不产生个性化买卖指令。
- [x] 不接入实盘下单接口。
- [x] 呈现推理链与反面风险，保持人在环内。

---

## 13. 测试策略

- **确定性计算**：全覆盖单测，用已知输入验证指标数值（防 §1.1 被破坏）。
- **数据层**：Provider 用录制的样本数据做契约测试，不打真网。
- **Agent 层**：mock tool 结果，验证编排流程与 ModelRouter 覆盖逻辑，不打真 LLM。

---

## 14. 待定 / 开放问题

1. ~~深度报告扩展思考~~ → 暂未接入，留后（默认关）。
2. ~~情绪面数据源~~ → 已定：美股 yfinance news、A股/港股东财 `stock_news_em`；情感打分列为可选增强。
3. ~~报告落盘~~ → 已实现 markdown 导出（`analyze -o`）；pdf 留后。
4. 非 Claude 模型 → 仍留 `LLMProvider` 扩展点，未实现（当前经中转已能用各种 Claude 兼容模型）。

**后续可选增强**：港股估值补全、新闻情感打分、`portfolio` 的 LLM 风险解读与再平衡建议、组合自定义权重（`--weights`）。
