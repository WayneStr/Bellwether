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
# 覆盖率门槛 76：M1-D2/D6/E4 落地后实测 ~78%（按验收规则锁到实际值-2，防回退）；
# 提到 85 需补 CLI 命令路径与 provider fundamentals/news 测例（M1 收尾或 M2 顺带）。
uv run pytest --cov=bellwether --cov-fail-under=76
