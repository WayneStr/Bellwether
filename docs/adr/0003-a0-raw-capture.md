# ADR-0003：A0 快照改为「事实层双抓」（raw + 复权视图）

- 状态：Accepted · 日期：2026-07-16 · 决策人：Fable（主编排），依 ROADMAP A3「存原始价格+公司行动、复权现算」既定原则
- 背景：RFC-002 评审（OPEN-1/D12）指出 A0 原实现只落复权视图（CN/HK qfq、US auto_adjust）。复权序列会因未来分红/送转发生全序列重写——只有不复权 raw 是不可重写的事实观测流，每晚一天改造就少积累一天。该方向为 ROADMAP 已定原则（两位独立评审共识），属按计划纠偏而非新决策，故先实施后补 ADR，可推翻。

## 决策

1. Provider 协议 `get_ohlcv` 增加 `adjust` 参数：`"default"`=复权视图（现行为，分析模块继续用）；`"raw"`=不复权原始价。三个 provider（yfinance/A股东财/港股新浪）均已实现；缓存键包含 `adjust` 防串扰。
2. US raw 模式额外携带 dividends / stock splits 事件列（yfinance `actions=True`），即公司行动的第一批原始观测。
3. A0 快照对每标的双抓：`ohlcv.csv`（视图，保持连续性）+ `ohlcv_raw.csv`（事实层）；manifest `SCHEMA_VERSION` 1→2，file 条目新增 `adjust` 标注。
4. 既有 v1 快照（仅 2026-07-16 当日 smoke 数据）保留原样，视作「视图观测」，不回填不删除。
5. A股/港股的复权因子暂不单独抓取——可由 raw 与 qfq 序列对拍推导；正式公司行动数据源属 M3 A3/A5 范围（对账口径 ground truth 尚为 RFC-002 OPEN-2，待裁决）。

## 验证记录（2026-07-16）
- pytest 52/52 全绿。
- 真实 smoke US+HK 6/6 成功；AAPL `ohlcv_raw.csv` 274 行、含 dividends 与 stock splits 列；manifest `schema_version=2`、四类文件（ohlcv/ohlcv_raw/fundamentals/news）齐全。
