"""测试共用辅助函数。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from http_boundary import Limits, Mode, ParseError, RequestParser, STRICT
from http_boundary.parser import RequestEvent

FAILURE_DIR = Path(__file__).parent / "fuzz_failures"


def run(
    data: bytes,
    mode: Mode = STRICT,
    limits: Limits | None = None,
    chunks: list[bytes] | None = None,
) -> tuple[list[RequestEvent], ParseError | None]:
    """解析整段数据，返回 (事件列表, 错误)。出错时保留出错前的事件。"""
    parser = RequestParser(mode, limits)
    events: list[RequestEvent] = []
    error: ParseError | None = None
    try:
        for piece in chunks if chunks is not None else [data]:
            events.extend(parser.feed(piece))
        events.extend(parser.finish())
    except ParseError as exc:
        events.extend(parser.drain_events())
        error = exc
    return events, error


def error_signature(error: ParseError | None) -> tuple:
    if error is None:
        return (None, None, None)
    return (type(error).__name__, error.message, error.offset)


def outcome_equal(
    first: tuple[list[RequestEvent], ParseError | None],
    second: tuple[list[RequestEvent], ParseError | None],
) -> bool:
    return first[0] == second[0] and error_signature(first[1]) == error_signature(
        second[1]
    )


def save_failure_case(mode: Mode, data: bytes, chunks: list[bytes]) -> Path:
    """把复现失败的输入保存下来，供 tests.test_fuzz 的重放测试加载。"""
    FAILURE_DIR.mkdir(exist_ok=True)
    digest = hashlib.sha256(mode.name.encode() + b"\0" + data).hexdigest()[:12]
    path = FAILURE_DIR / f"case-{mode.name}-{digest}.json"
    path.write_text(
        json.dumps(
            {
                "mode": mode.name,
                "data": data.hex(),
                "chunks": [piece.hex() for piece in chunks],
            }
        )
    )
    return path
