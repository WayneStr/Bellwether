# Security Policy

## 支持版本

0.x 系列仅最新版本接收安全修复。

## 报告漏洞

请**不要**在公开 issue 中披露安全问题。

请走 GitHub Security Advisories 私密通道上报：仓库 **Security** 标签页 →
**Report a vulnerability**（[直达链接](https://github.com/WayneStr/Bellwether/security/advisories/new)）。
该通道对外不可见，仅维护者可读。

我们会在 7 天内确认收到，并在修复发布前与你协调披露时间。

## 密钥管理

- `ANTHROPIC_API_KEY` 只从**环境变量**或**系统钥匙串**（`bellwether config set-key`，
  需 `keyring` 可选依赖）读取；环境变量优先。
- 密钥**绝不**写入 `config.toml`、代码、日志或任何仓库文件。
- 所有用户可见的错误输出与落盘文本经统一脱敏（`core/redact.py`）：已知密钥值
  精确替换 + `sk-` 前缀模式兜底（防劣质中转在错误 body 中回显 key）。

## 数据合规

数据源许可边界与云备份条件见 `docs/data-licensing.md`（ADR-0005）。
原始行情快照仅本地私存；公开再分发受源站条款约束，红线清单见该文档。

## 产品边界（永久红线）

本项目**不**执行实盘下单/自动交易，**不**提供个性化买卖建议；输出仅为
分析、依据、情景与风险，且附免责声明。任何试图移除这些边界的补丁不会被接受。
