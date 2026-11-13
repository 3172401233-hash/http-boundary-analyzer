"""解析过程的资源上限配置。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    """解析资源上限。字段数限制同时适用于头部与 trailer。

    max_header_bytes:  请求行 + 头部块（含 CRLF）的最大字节数。
    max_header_fields: 头部字段（或 trailer 字段）的最大数量。
    max_body_bytes:    单个请求正文的最大字节数（chunked 按解码后累计）。
    max_chunks:        单个请求的最大 chunk 数量。
    max_line_bytes:    任意一行（请求行、头部行、chunk-size 行）的最大字节数。
    max_buffer_bytes:  解析器内部未消费缓冲的最大字节数，防止持续碎片输入撑爆内存。
    """

    max_header_bytes: int = 16_384
    max_header_fields: int = 100
    max_body_bytes: int = 8 * 1024 * 1024
    max_chunks: int = 10_000
    max_line_bytes: int = 8_192
    max_buffer_bytes: int = 1_048_576
