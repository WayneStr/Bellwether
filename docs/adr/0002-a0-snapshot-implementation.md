# ADR-0002：A0 每日快照任务的实施决策

- 状态：Accepted · 日期：2026-07-16 · 决策人：Fable（主编排），依 ROADMAP A0 授权
- 背景：A0 是「时间物理」资产（新闻等免费源无法回填历史），M0 放行后立即上线。以下为实施级决策。

## 决策

1. **黄金集构成**（`bellwether/golden_set.toml`）：30×3 市场，各市场市值/流动性头部+行业分散；快照与评测黄金集（C2）共用。构成可调，调整须新 ADR 注明日期（影响跨日可比性）。
2. **存放**：`~/.bellwether/snapshots/YYYY-MM-DD/{MARKET}/{SYMBOL}/`，**项目外、绝不入 git**。理由：yfinance/东财/新浪原始数据入公开仓库构成再分发（法律暴露，评审 F3/#3）。本地私有=不再分发，天然合规；正式存放与再分发策略 M1 E3 裁决，届时若变更，本目录整体迁移。
3. **原始层格式**：ohlcv.csv + fundamentals.json + news.json（provider 清洗后的最接近原始形态）；每文件 sha256 + 字节数入 manifest；`schema_version=1`。不用 parquet：避免 P0 期新增 pyarrow 重依赖，M3 迁 DuckDB/Parquet（迁移方案属 RFC-002 必答）。
4. **manifest 语义**：单标的/单数据类失败记 `failures` 不中断全局；退出码 0=全成 / 2=部分失败（降级）/ 1=全军覆没。smoke 运行写 `manifest-smoke.json` 且不更新 `last_status.json`——手工冒烟不得污染每日全量任务的告警面。
5. **礼貌限流**：标的间 `delay + U(0,0.3)` 秒（默认 0.7s），全量 90 标的约 3–8 分钟——对免费源的主动节流（评审 F13）。
6. **调度**：macOS launchd 用户级 agent（`com.bellwether.snapshot`，每日本地 08:30——A股/港股昨日收盘与美股当日凌晨收盘均已可得；launchd 对睡眠错过的 calendar 任务会在唤醒后补跑，优于 cron）。plist 副本与安装/卸载说明在 `scripts/`。
7. **告警面**：`last_status.json`（机器可读）+ stderr 日志 `~/.bellwether/logs/`。M1 D2/D3 落地后接入正式告警。

## 验证记录（2026-07-16）
- 单测 5 项（落盘/manifest/sha256/失败记录/smoke 隔离/退出码）全绿，总 52/52。
- 真实 smoke：US+HK 6/6 成功（AAPL 274 行 K 线、00700 新闻 10 条）；CN 3/3 失败且 exit=1——CN 东财在验证沙箱不可达属预期（用户网络可达），恰好完整演练了告警路径。
- launchd 已装载（state=not running，待 08:30 触发）。

## 遗留
- 首次全量（90 标的）在用户网络跑通尚待确认（沙箱 CN 不可达）；明早 08:30 首跑或手动 `launchctl kickstart`。
