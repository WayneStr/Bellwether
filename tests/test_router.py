"""ModelRouter 三级覆盖逻辑的单测（不打网络、不需要 API key）。"""

import pytest

from bellwether.agent.router import ModelRouter
from bellwether.models import ModelConfig


def _router() -> ModelRouter:
    return ModelRouter(ModelConfig())  # 内置默认


def test_resolve_default_role():
    assert _router().resolve("deep_report").model == "claude-opus-4-8"


def test_cli_model_override_wins():
    spec = _router().resolve("deep_report", model="claude-sonnet-5")
    assert spec.model == "claude-sonnet-5"


def test_param_override():
    spec = _router().resolve("synthesis", temperature=0.9, max_tokens=1234)
    assert spec.params.temperature == 0.9
    assert spec.params.max_tokens == 1234


def test_override_does_not_mutate_config():
    cfg = ModelConfig()
    ModelRouter(cfg).resolve("deep_report", model="claude-sonnet-5", temperature=0.99)
    # 覆盖是拷贝，原 config 不受污染
    assert cfg.deep_report.model == "claude-opus-4-8"
    assert cfg.deep_report.params.temperature == 0.3


def test_none_overrides_ignored():
    spec = _router().resolve("parse", temperature=None, model=None)
    assert spec.model == "claude-haiku-4-5-20251001"


def test_unknown_role_raises():
    with pytest.raises(ValueError):
        _router().resolve("nonexistent")
