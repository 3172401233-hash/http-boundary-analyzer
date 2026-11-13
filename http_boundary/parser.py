"""增量式 HTTP/1.1 请求边界解析器。

解析器按字节流工作：可以任意分片调用 feed()，解析结果与分片方式无关。
已消费的数据不会重复扫描（行搜索从上次失败位置继续），持续碎片输入是
摊还 O(1) 的。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import LimitExceeded, ParseError
from .limits import Limits
from .modes import (
    CONFLICT_REJECT,
    DUP_FIRST,
    DUP_REJECT,
    Mode,
    STRICT,
)

_TOKEN_RE = re.compile(rb"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_REQUEST_LINE_RE = re.compile(
    rb"^([!#$%&'*+\-.^_`|~0-9A-Za-z]+) ([^\s]+) (HTTP/1\.[01])$"
)
_HEX_DIGITS = frozenset(b"0123456789abcdefABCDEF")

_COMPACT_THRESHOLD = 65_536


class _NeedMore(Exception):
    """内部信号：当前缓冲区数据不足，需要更多输入。"""


@dataclass(frozen=True)
class RequestEvent:
    """一个完整请求的解析结果。所有偏移均为整个输入流的绝对偏移。

    body_start/body_end: 正文区段的半开区间 [start, end)。
        固定长度正文即正文本身；chunked 正文从第一个 chunk-size 行起，
        到 trailer 结束的空行为止（即传输层上的正文区段）。
    body_length: 解码后的正文长度（固定长度即声明值，chunked 为各块之和）。
    next_offset: 下一个请求的起始偏移（== 本请求消费完的位置）。
    framing:     "none" | "fixed" | "chunked"。
    """

    index: int
    method: str
    target: str
    version: str
    headers: tuple[tuple[str, str], ...]
    trailers: tuple[tuple[str, str], ...]
    framing: str
    body_start: int
    body_end: int
    body_length: int
    next_offset: int


class RequestParser:
    """按指定模式解析 HTTP/1.1 请求字节流。

    用法：
        parser = RequestParser(mode)
        events = parser.feed(b"...")   # 可多次调用
        events += parser.finish()      # 输入结束；截断会抛 ParseError
    """

    def __init__(self, mode: Mode = STRICT, limits: Limits | None = None):
        self.mode = mode
        self.limits = limits or Limits()
        self._buf = bytearray()
        self._base = 0   # _buf[0] 对应的绝对偏移
        self._pos = 0    # 读游标（相对 _buf）
        self._scan = 0   # 行搜索起点（之前的区间已确认没有 LF）
        self._state = "headers"
        self._events: list[RequestEvent] = []
        self._index = 0
        self._finished = False
        self._reset_message()

    # ------------------------------------------------------------------ API

    def feed(self, data: bytes) -> list[RequestEvent]:
        """喂入一段数据，返回本次新完成的请求事件。"""
        if self._finished:
            raise ParseError("feed() called after finish()", self._base + self._pos)
        if not data:
            return []
        self._maybe_compact()
        self._buf += data
        try:
            self._pump()
        except _NeedMore:
            if len(self._buf) - self._pos > self.limits.max_buffer_bytes:
                raise LimitExceeded(
                    "buffered data exceeds max_buffer_bytes",
                    self._base + self._pos,
                ) from None
        return self._drain_events()

    def drain_events(self) -> list[RequestEvent]:
        """取出已缓冲但尚未返回的事件。

        feed() 抛出 ParseError/LimitExceeded 时，本次调用中已完成的请求
        事件仍留在解析器内，调用本方法取回，保证不丢失。
        """
        return self._drain_events()

    def finish(self) -> list[RequestEvent]:
        """声明输入结束。若处于请求中途则抛出 ParseError（截断）。"""
        self._finished = True
        clean = (
            self._state == "headers"
            and self._request_line is None
            and not self._header_fields
            and not self._buf[self._pos:]
        )
        if not clean:
            raise ParseError("truncated input", self._base + self._pos)
        return self._drain_events()

    # ------------------------------------------------------------- 内部状态

    def _reset_message(self) -> None:
        self._request_line: tuple[str, str, str] | None = None
        self._header_fields: list[tuple[str, str]] = []
        self._header_bytes = 0
        self._body_bytes = 0
        self._body_start = 0
        self._body_remaining = 0
        self._chunk_count = 0
        self._chunk_remaining = 0
        self._trailers: list[tuple[str, str]] = []
        self._framing = "none"

    def _drain_events(self) -> list[RequestEvent]:
        events, self._events = self._events, []
        return events

    def _maybe_compact(self) -> None:
        if self._pos >= _COMPACT_THRESHOLD and self._pos >= len(self._buf) // 2:
            del self._buf[: self._pos]
            self._base += self._pos
            self._pos = 0
            self._scan = 0

    def _consume(self, n: int) -> None:
        self._pos += n
        self._scan = self._pos

    def _read_line(self) -> tuple[bytes, int]:
        """读取一行（不含 CRLF），返回 (行内容, 行起始绝对偏移)。"""
        idx = self._buf.find(b"\n", max(self._scan, self._pos))
        if idx < 0:
            if len(self._buf) - self._pos > self.limits.max_line_bytes:
                raise LimitExceeded(
                    "line exceeds max_line_bytes", self._base + self._pos
                )
            self._scan = len(self._buf)
            raise _NeedMore
        line = bytes(self._buf[self._pos:idx])
        offset = self._base + self._pos
        self._consume(idx + 1 - self._pos)
        if not line.endswith(b"\r"):
            raise ParseError("bare LF line ending (expected CRLF)", self._base + idx)
        line = line[:-1]
        bare_cr = line.find(b"\r")
        if bare_cr >= 0:
            raise ParseError("bare CR inside line", offset + bare_cr)
        return line, offset

    # ------------------------------------------------------------ 状态机

    def _pump(self) -> None:
        while True:
            if self._state == "headers":
                self._parse_headers()
            elif self._state == "fixed":
                self._parse_fixed()
            elif self._state == "chunk_size":
                self._parse_chunk_size()
            elif self._state == "chunk_data":
                self._parse_chunk_data()
            elif self._state == "chunk_crlf":
                self._parse_chunk_crlf()
            elif self._state == "trailer":
                self._parse_trailer()
            else:  # pragma: no cover - 防御
                raise AssertionError(f"unknown state {self._state!r}")

    def _parse_headers(self) -> None:
        while True:
            line, offset = self._read_line()
            self._header_bytes += len(line) + 2
            if self._header_bytes > self.limits.max_header_bytes:
                raise LimitExceeded("header block too large", offset)
            if self._request_line is None:
                self._parse_request_line(line, offset)
                continue
            if line == b"":
                self._finish_headers(offset + 2)
                return
            self._header_fields.append(self._parse_field(line, offset))
            if len(self._header_fields) > self.limits.max_header_fields:
                raise LimitExceeded("too many header fields", offset)

    def _parse_request_line(self, line: bytes, offset: int) -> None:
        match = _REQUEST_LINE_RE.match(line)
        if match is None:
            raise ParseError("malformed request line", offset)
        self._request_line = (
            match.group(1).decode("ascii"),
            match.group(2).decode("latin-1"),
            match.group(3).decode("ascii"),
        )

    @staticmethod
    def _parse_field(line: bytes, offset: int) -> tuple[str, str]:
        if line[:1] in (b" ", b"\t"):
            raise ParseError("obsolete line folding is not accepted", offset)
        name, sep, value = line.partition(b":")
        if not sep or not _TOKEN_RE.match(name):
            raise ParseError("malformed header line", offset)
        if any((c < 0x20 and c != 0x09) or c == 0x7F for c in value):
            raise ParseError("control character in header value", offset)
        return name.decode("ascii"), value.strip().decode("latin-1")

    def _finish_headers(self, end_offset: int) -> None:
        """头部块结束，根据模式规则决定正文边界解析方式。"""
        headers = self._header_fields
        cl_values = [v for n, v in headers if n.lower() == "content-length"]
        te_values = [v for n, v in headers if n.lower() == "transfer-encoding"]

        codings = [
            coding.strip().lower()
            for value in te_values
            for coding in value.split(",")
        ]
        codings = [c for c in codings if c]
        chunked = False
        if codings:
            for coding in codings:
                if coding not in self.mode.allowed_transfer_codings:
                    raise ParseError(
                        f"unsupported transfer coding: {coding!r}",
                        end_offset,
                        fields=("transfer-encoding",),
                    )
            if codings[-1] != "chunked":
                raise ParseError(
                    "transfer-encoding does not end with 'chunked'",
                    end_offset,
                    fields=("transfer-encoding",),
                )
            chunked = True

        lengths = []
        for value in cl_values:
            if not value or any(c < "0" or c > "9" for c in value):
                raise ParseError(
                    "invalid Content-Length value",
                    end_offset,
                    fields=("content-length",),
                )
            lengths.append(int(value))

        length: int | None = None
        if lengths:
            if len(lengths) > 1:
                rule = self.mode.duplicate_content_length
                if rule == DUP_REJECT:
                    raise ParseError(
                        "duplicate Content-Length fields",
                        end_offset,
                        fields=("content-length",),
                    )
                length = lengths[0] if rule == DUP_FIRST else lengths[-1]
            else:
                length = lengths[0]

        if chunked and cl_values:
            if self.mode.cl_te_conflict == CONFLICT_REJECT:
                raise ParseError(
                    "Content-Length combined with Transfer-Encoding",
                    end_offset,
                    fields=("content-length", "transfer-encoding"),
                )
            length = None  # chunked 优先，忽略 Content-Length

        self._body_start = end_offset
        if chunked:
            self._framing = "chunked"
            self._state = "chunk_size"
        elif length is not None:
            if length > self.limits.max_body_bytes:
                raise LimitExceeded(
                    "declared Content-Length exceeds max_body_bytes",
                    end_offset,
                    fields=("content-length",),
                )
            self._framing = "fixed"
            self._body_remaining = length
            self._state = "fixed"
        else:
            self._framing = "none"
            self._emit(end_offset)

    def _parse_fixed(self) -> None:
        if self._body_remaining == 0:
            self._emit(self._base + self._pos)
            return
        available = len(self._buf) - self._pos
        if available == 0:
            raise _NeedMore
        take = min(available, self._body_remaining)
        self._consume(take)
        self._body_remaining -= take
        self._body_bytes += take
        if self._body_remaining == 0:
            self._emit(self._base + self._pos)

    def _parse_chunk_size(self) -> None:
        line, offset = self._read_line()
        size_text, _, extension = line.partition(b";")
        size_text = size_text.strip()
        if not size_text or any(c not in _HEX_DIGITS for c in size_text):
            raise ParseError("invalid chunk size", offset)
        if any((c < 0x20 and c != 0x09) or c == 0x7F for c in extension):
            raise ParseError("control character in chunk extension", offset)
        size = int(size_text, 16)
        self._chunk_count += 1
        if self._chunk_count > self.limits.max_chunks:
            raise LimitExceeded("too many chunks", offset)
        if self._body_bytes + size > self.limits.max_body_bytes:
            raise LimitExceeded("chunked body exceeds max_body_bytes", offset)
        if size == 0:
            self._state = "trailer"
        else:
            self._chunk_remaining = size
            self._state = "chunk_data"

    def _parse_chunk_data(self) -> None:
        if self._chunk_remaining == 0:
            self._state = "chunk_crlf"
            return
        available = len(self._buf) - self._pos
        if available == 0:
            raise _NeedMore
        take = min(available, self._chunk_remaining)
        self._consume(take)
        self._chunk_remaining -= take
        self._body_bytes += take
        if self._chunk_remaining == 0:
            self._state = "chunk_crlf"

    def _parse_chunk_crlf(self) -> None:
        line, offset = self._read_line()
        if line != b"":
            raise ParseError("missing CRLF after chunk data", offset)
        self._state = "chunk_size"

    def _parse_trailer(self) -> None:
        while True:
            line, offset = self._read_line()
            if line == b"":
                self._emit(offset + 2)
                return
            self._trailers.append(self._parse_field(line, offset))
            if len(self._trailers) > self.limits.max_header_fields:
                raise LimitExceeded("too many trailer fields", offset)

    def _emit(self, end_offset: int) -> None:
        method, target, version = self._request_line  # type: ignore[misc]
        self._events.append(
            RequestEvent(
                index=self._index,
                method=method,
                target=target,
                version=version,
                headers=tuple(self._header_fields),
                trailers=tuple(self._trailers),
                framing=self._framing,
                body_start=self._body_start,
                body_end=end_offset,
                body_length=self._body_bytes,
                next_offset=end_offset,
            )
        )
        self._index += 1
        self._state = "headers"
        self._reset_message()
