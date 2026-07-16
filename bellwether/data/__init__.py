"""数据层：可插拔市场数据 provider。import 时注册内置 provider。"""

from . import yfinance_provider as _yf  # noqa: F401  触发 US provider 注册
from . import akshare_provider as _ak  # noqa: F401  触发 CN provider 注册
