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
# 覆盖率门槛 65：M1-D1 落地时实测 ~67%（不足 80，按验收规则降到实际值-2），
# 待 M1 后段补测例再提到 85。
uv run pytest --cov=bellwether --cov-fail-under=65
