"""http_boundary：HTTP/1.1 请求边界对照分析库。

离线分析同一段原始请求字节流在不同解析规则（模式）下是否会被切分出
不同的消息边界。只做静态模拟分析，不进行任何网络通信。
"""

from .analyzer import (
    AnalysisReport,
    Divergence,
    ModeOutcome,
    analyze,
    run_mode,
)
from .errors import LimitExceeded, ParseError
from .limits import Limits
from .modes import (
    CL_FIRST,
    CL_LAST,
    DEFAULT_MODES,
    STRICT,
    TE_PRIORITY,
    Mode,
    mode_by_name,
)
from .parser import RequestEvent, RequestParser

__all__ = [
    "AnalysisReport",
    "CL_FIRST",
    "CL_LAST",
    "DEFAULT_MODES",
    "Divergence",
    "LimitExceeded",
    "Limits",
    "Mode",
    "ModeOutcome",
    "ParseError",
    "RequestEvent",
    "RequestParser",
    "STRICT",
    "TE_PRIORITY",
    "analyze",
    "mode_by_name",
    "run_mode",
]

__version__ = "0.1.0"
