"""D6 安全工程单测：脱敏零泄漏 + keyring 读取路径。"""

import anthropic
import httpx

from bellwether.agent.llm import translate_anthropic_error
from bellwether.config import AppConfig, api_key_source
from bellwether.core.redact import redact

_FAKE_KEY = "sk-ant-test-1234567890abcdef"


# ─────────────────────────── redact ───────────────────────────
def test_redact_known_env_secret(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "relay-key-without-sk-prefix")
    out = redact("请求失败：key=relay-key-without-sk-prefix 无效")
    assert "relay-key-without-sk-prefix" not in out
    assert "***" in out


def test_redact_sk_pattern_without_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = redact(f"invalid x-api-key: {_FAKE_KEY}")
    assert _FAKE_KEY not in out
    assert "sk-***" in out


def test_redact_plain_text_untouched():
    text = "东财与新浪均失败：em=断连; sina=断连"
    assert redact(text) == text


def test_llm_error_message_is_redacted(monkeypatch):
    # 劣质中转在错误 body 里回显收到的 key → 翻译后的消息必须不含明文
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    request = httpx.Request("POST", "https://api.test/v1/messages")
    response = httpx.Response(401, request=request, json={})
    exc = anthropic.AuthenticationError(
        f"invalid x-api-key {_FAKE_KEY}",
        response=response,
        body={"error": {"message": f"invalid x-api-key {_FAKE_KEY}"}},
    )
    translated = translate_anthropic_error(exc)
    assert _FAKE_KEY not in str(translated)


# ─────────────────────────── keyring ───────────────────────────
def test_env_key_takes_priority(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    monkeypatch.setattr("keyring.get_password", lambda service, user: "keyring-key", raising=False)
    assert AppConfig().anthropic_api_key == "env-key"
    assert api_key_source() == "env"


def test_keyring_fallback_when_env_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def fake_get(service, user):
        assert (service, user) == ("bellwether", "anthropic_api_key")
        return "keyring-key"

    monkeypatch.setattr("keyring.get_password", fake_get, raising=False)
    assert AppConfig().anthropic_api_key == "keyring-key"
    assert api_key_source() == "keyring"


def test_keyring_backend_failure_is_tolerated(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def broken(service, user):
        raise RuntimeError("无可用 keyring 后端（headless 环境）")

    monkeypatch.setattr("keyring.get_password", broken, raising=False)
    assert AppConfig().anthropic_api_key is None
    assert api_key_source() == "none"
