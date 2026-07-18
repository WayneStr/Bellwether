"""输出脱敏（ROADMAP D6）：用户可见文本与落盘文本经此清洗，密钥零泄漏。

两层防线：已知 secret（环境变量里的真实 key 值）精确替换 + sk- 前缀模式兜底
（劣质中转可能在错误 body 里回显收到的 key）。
"""

from __future__ import annotations

import os
import re

_SK_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")
_ENV_SECRETS = ("ANTHROPIC_API_KEY",)


def redact(text: str) -> str:
    for var in _ENV_SECRETS:
        value = os.environ.get(var)
        if value and value in text:
            text = text.replace(value, "***")
    return _SK_PATTERN.sub("sk-***", text)
