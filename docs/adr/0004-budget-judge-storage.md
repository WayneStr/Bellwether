# ADR-0004：预算总账、B2 裁决协议、快照存储的三项裁决

- 状态：Accepted · 日期：2026-07-17 · 决策人：维护者（WayneStr），经双评审 RFC 修订稿（RFC-000 §7 联动表）裁决
- 背景：RFC 初稿的 9 个高优 [OPEN] 经 deep-reasoner + Codex 双盲评审与修订收敛为 3 项维护者裁决（[OPEN-BUDGET]/[OPEN-JUDGE]/[OPEN-STORAGE]），2026-07-17 以选择题裁决。

三项裁决：
1. **[OPEN-BUDGET] 按建议值通过**：生产单次硬上限 quick ≤$0.35、deep ≤$1.50（token 与 USD 双计量，先到先触发；$1.50 隐含 prompt caching 生效，列为 M4 定标显式验证项，名义无 caching 上界 $3.64 见 RFC-001 D6a）；月度评测封套降档起步 ≈$0.9–1.5k/月（PR 抽样 + 周期评测 + release + 噪声重测），O7 实测单价后再议升档（上界 ≈$2.4k/月）；B2 受控实验一次性 ≤$1,500（三臂，不混入月度）。三本账共享同一 price-book（ROADMAP-D3，内置+可覆盖+版本化）。
2. **[OPEN-JUDGE] 轻量档**：B2 人工裁决由维护者自任 + 对臂盲化（报告去标识随机呈现）+ rubric 预注册 + 争议样本外部复核；预估 3–4 人日，超量时按预注册的分层抽样降级。
3. **[OPEN-STORAGE] 本地主存，E3 后接云备份**：cassette 与 A0 快照现阶段纯本地（公开仓库仅放 manifest 哈希与 recorder 脚本）；E3 许可审计（M1）书面确认后立即启用加密云备份；窗口期接受单点风险。fork PR 无 secrets，PR 层评测仅在维护者机器/自托管 runner 运行，公开 CI 只跑守门员测试。

后果：RFC-000 §7 表格数值生效；M2 评测与 M4 B2 的预算按此执行；变更需新 ADR。
