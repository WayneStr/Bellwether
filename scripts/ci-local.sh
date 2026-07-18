#!/usr/bin/env bash
# 本地版 CI（与 .github/workflows/ci.yml 步骤等价）：ruff check → mypy src → pytest --cov。
# 用 uv run 保证工具来自项目环境（不依赖 shell PATH），与 CI 完全一致。
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== ruff check =="
uv run ruff check src tests

echo "== mypy src =="
uv run mypy src

echo "== pytest (coverage) =="
# 覆盖率门槛 85：M1 收尾补齐 CLI 命令路径（cli.py 0%→90%）与 provider fundamentals/news
# 测例（yfinance/akshare）后实测 ~92%（按验收规则锁到 85，留安全边际，防回退）。
uv run pytest --cov=bellwether --cov-fail-under=85
