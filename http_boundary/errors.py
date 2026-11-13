"""错误类型：明确区分格式错误与资源超限。"""

from __future__ import annotations


class ParseError(Exception):
    """格式错误：输入字节不可能构成合法（或被当前模式接受）的 HTTP/1.1 请求。

    属性：
        message: 人类可读的原因描述。
        offset:  错误位置在整个输入流中的绝对偏移（从 0 开始）。
        fields:  相关的头部字段名（小写），用于分歧报告定位原因。
    """

    def __init__(self, message: str, offset: int, fields: tuple[str, ...] = ()):
        super().__init__(message)
        self.message = message
        self.offset = offset
        self.fields = fields

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return f"{type(self).__name__}({self.message!r}, offset={self.offset})"


class LimitExceeded(ParseError):
    """资源超限：头部、正文、块数量、行长度或缓冲占用超过配置上限。"""
