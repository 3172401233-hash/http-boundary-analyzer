"""跨模式对照分析：同一段字节流在不同解析规则下的边界差异。

analyze() 用每个模式独立解析同一份数据，然后对所有模式两两比较：
接受/拒绝结果、每个请求的正文范围、下一请求起点。发现分歧时给出
原始数据中的绝对偏移、相关头部字段和产生差异的规则原因。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ParseError
from .limits import Limits
from .modes import DEFAULT_MODES, Mode
from .parser import RequestEvent, RequestParser


@dataclass(frozen=True)
class ModeOutcome:
    """单个模式对一份输入的完整解析结果。

    requests: 成功解析出的请求（出错前已完成的请求也会保留）。
    error:    解析失败时的异常（ParseError 或 LimitExceeded），成功为 None。
    """

    mode: str
    requests: tuple[RequestEvent, ...]
    error: ParseError | None


@dataclass(frozen=True)
class Divergence:
    """两个模式之间的一处分歧。

    kind:   "accept-reject" | "error-detail" | "boundary" | "request-count"。
    offset: 分歧在原始数据中的（近似起始）绝对偏移。
    fields: 相关的头部字段名。
    reason: 产生差异的原因说明（含规则差异与数据特征）。
    details: 每个模式各自的结果描述。
    """

    kind: str
    offset: int
    fields: tuple[str, ...]
    reason: str
    details: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AnalysisReport:
    """对照分析结果。"""

    outcomes: dict[str, ModeOutcome]
    divergences: tuple[Divergence, ...]

    @property
    def consistent(self) -> bool:
        """所有模式对边界的判定完全一致时为 True。"""
        return not self.divergences


def run_mode(
    mode: Mode,
    data: bytes,
    limits: Limits | None = None,
    chunks: list[bytes] | None = None,
) -> ModeOutcome:
    """用单个模式解析整段数据；chunks 可指定分片方式。"""
    parser = RequestParser(mode, limits)
    events: list[RequestEvent] = []
    try:
        for piece in chunks if chunks is not None else [data]:
            events.extend(parser.feed(piece))
        events.extend(parser.finish())
    except ParseError as exc:
        events.extend(parser.drain_events())
        return ModeOutcome(mode.name, tuple(events), exc)
    return ModeOutcome(mode.name, tuple(events), None)


def analyze(
    data: bytes,
    modes: tuple[Mode, ...] = DEFAULT_MODES,
    limits: Limits | None = None,
) -> AnalysisReport:
    """用所有模式解析同一份数据并比较边界判定。"""
    if not modes:
        raise ValueError("at least one mode is required")
    outcomes = {m.name: run_mode(m, data, limits) for m in modes}
    divergences: list[Divergence] = []
    for i, mode_a in enumerate(modes):
        for mode_b in modes[i + 1:]:
            divergences.extend(
                _compare(
                    outcomes[mode_a.name],
                    outcomes[mode_b.name],
                    mode_a,
                    mode_b,
                )
            )
    return AnalysisReport(outcomes, tuple(divergences))


_DUP_TEXT = {
    "reject": "拒绝重复 Content-Length",
    "first": "重复 Content-Length 取第一个值",
    "last": "重复 Content-Length 取最后一个值",
}
_CONFLICT_TEXT = {
    "reject": "拒绝 Content-Length 与 Transfer-Encoding 并存",
    "chunked_priority": "并存时忽略 Content-Length 按 chunked 解析",
}


def _rule_diff(a: Mode, b: Mode) -> str:
    parts = []
    if a.duplicate_content_length != b.duplicate_content_length:
        parts.append(
            f"{a.name} {_DUP_TEXT[a.duplicate_content_length]}，"
            f"{b.name} {_DUP_TEXT[b.duplicate_content_length]}"
        )
    if a.cl_te_conflict != b.cl_te_conflict:
        parts.append(
            f"{a.name} {_CONFLICT_TEXT[a.cl_te_conflict]}，"
            f"{b.name} {_CONFLICT_TEXT[b.cl_te_conflict]}"
        )
    return "；".join(parts) if parts else "两模式规则相同"


def _describe(outcome: ModeOutcome) -> str:
    if outcome.error is not None:
        kind = (
            "资源超限" if type(outcome.error).__name__ == "LimitExceeded" else "格式错误"
        )
        return (
            f"拒绝（{kind}：{outcome.error.message}，"
            f"偏移 {outcome.error.offset}），此前已接受 {len(outcome.requests)} 个请求"
        )
    spans = ", ".join(f"[{r.body_start}, {r.body_end})" for r in outcome.requests)
    return f"接受 {len(outcome.requests)} 个请求，正文区间 {spans or '无'}"


def _cause_hint(event: RequestEvent) -> str:
    names = [n.lower() for n, _ in event.headers]
    hints = []
    cl_count = names.count("content-length")
    if cl_count > 1:
        hints.append(f"请求带有 {cl_count} 个 Content-Length 字段")
    if "content-length" in names and "transfer-encoding" in names:
        hints.append("Content-Length 与 Transfer-Encoding 并存")
    return "；".join(hints)


def _boundary_key(event: RequestEvent) -> tuple:
    return (
        event.body_start,
        event.body_end,
        event.body_length,
        event.next_offset,
        event.framing,
    )


def _compare(
    ref: ModeOutcome,
    other: ModeOutcome,
    ref_mode: Mode,
    other_mode: Mode,
) -> list[Divergence]:
    rules = _rule_diff(ref_mode, other_mode)
    details = {ref.mode: _describe(ref), other.mode: _describe(other)}

    if (ref.error is None) != (other.error is None):
        error = ref.error or other.error
        assert error is not None
        return [
            Divergence(
                kind="accept-reject",
                offset=error.offset,
                fields=error.fields,
                reason=(
                    f"一个模式接受而另一个拒绝：{error.message}。规则差异：{rules}。"
                ),
                details=details,
            )
        ]

    if ref.error is not None and other.error is not None:
        same = (
            type(ref.error) is type(other.error)
            and ref.error.message == other.error.message
            and ref.error.offset == other.error.offset
        )
        if same:
            return []
        return [
            Divergence(
                kind="error-detail",
                offset=min(ref.error.offset, other.error.offset),
                fields=ref.error.fields or other.error.fields,
                reason=(
                    f"两模式都拒绝，但错误不同：{ref.mode}: {ref.error.message}"
                    f"（偏移 {ref.error.offset}）；{other.mode}: "
                    f"{other.error.message}（偏移 {other.error.offset}）。"
                ),
                details=details,
            )
        ]

    common = 0
    for ref_req, other_req in zip(ref.requests, other.requests):
        if _boundary_key(ref_req) != _boundary_key(other_req):
            hint = _cause_hint(other_req) or _cause_hint(ref_req)
            return [
                Divergence(
                    kind="boundary",
                    offset=min(ref_req.body_start, other_req.body_start),
                    fields=tuple(
                        dict.fromkeys(
                            n.lower()
                            for n, _ in ref_req.headers + other_req.headers
                            if n.lower() in ("content-length", "transfer-encoding")
                        )
                    ),
                    reason=(
                        f"第 {ref_req.index} 个请求的消息边界不同："
                        f"{ref.mode} 判定正文 [{ref_req.body_start}, "
                        f"{ref_req.body_end}) 下一请求起点 {ref_req.next_offset}；"
                        f"{other.mode} 判定正文 [{other_req.body_start}, "
                        f"{other_req.body_end}) 下一请求起点 {other_req.next_offset}。"
                        f"{('数据特征：' + hint + '。') if hint else ''}"
                        f"规则差异：{rules}。"
                    ),
                    details=details,
                )
            ]
        common += 1

    if len(ref.requests) != len(other.requests):
        offset = ref.requests[common - 1].next_offset if common else 0
        longer = ref if len(ref.requests) > len(other.requests) else other
        hint = _cause_hint(longer.requests[common])
        return [
            Divergence(
                kind="request-count",
                offset=offset,
                fields=tuple(
                    dict.fromkeys(
                        n.lower()
                        for n, _ in longer.requests[common].headers
                        if n.lower() in ("content-length", "transfer-encoding")
                    )
                ),
                reason=(
                    f"请求数量不同：{ref.mode} 接受 {len(ref.requests)} 个，"
                    f"{other.mode} 接受 {len(other.requests)} 个；从偏移 {offset} "
                    f"起判定分叉。{('数据特征：' + hint + '。') if hint else ''}"
                    f"规则差异：{rules}。"
                ),
                details=details,
            )
        ]

    return []
