"""structlog 接线（M2-D3 可观测性）：--verbose 时事件以 pretty console 输出到 stderr；
默认 WARNING 静默（orchestrator 事件按 info 级打，不加 --verbose 不出声）。
"""

from __future__ import annotations

import logging
import sys

import structlog


def setup_logging(verbose: bool) -> None:
    """配置全局 structlog。须在打日志前调用一次（CLI 入口负责）。

    不缓存已绑定的 logger（cache_logger_on_first_use 默认 False）：orchestrator 的
    `_log` 是模块级单例，若缓存，同一进程内先后以不同 verbose 值调用本函数时，
    第一次实际打日志时的级别会「锁死」，后续调用形同虚设。
    """
    level = logging.INFO if verbose else logging.WARNING
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )
