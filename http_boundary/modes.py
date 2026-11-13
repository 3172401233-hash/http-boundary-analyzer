"""解析模式定义。

每个模式是一组明确的判定规则。兼容模式只放松某一条规则，其余行为与
严格模式一致；这些模式是对照用的假想解析器，不对应任何真实服务器产品。
"""

from __future__ import annotations

from dataclasses import dataclass

DUP_REJECT = "reject"  # 出现重复 Content-Length 即拒绝（即使值相同）
DUP_FIRST = "first"    # 取第一个 Content-Length 值
DUP_LAST = "last"      # 取最后一个 Content-Length 值

CONFLICT_REJECT = "reject"                      # CL 与 TE 并存即拒绝
CONFLICT_CHUNKED_PRIORITY = "chunked_priority"  # 忽略 Content-Length，按 chunked 解析


@dataclass(frozen=True)
class Mode:
    """一组请求边界判定规则。

    duplicate_content_length: 重复 Content-Length 的策略（reject/first/last）。
    cl_te_conflict:           CL 与 TE 并存时的策略（reject/chunked_priority）。
    allowed_transfer_codings: 允许的传输编码（所有模式默认只接受 chunked）。
    """

    name: str
    duplicate_content_length: str = DUP_REJECT
    cl_te_conflict: str = CONFLICT_REJECT
    allowed_transfer_codings: tuple[str, ...] = ("chunked",)

    def rule_summary(self) -> str:
        """返回该模式规则的中文描述。"""
        dup_text = {
            DUP_REJECT: "拒绝重复的 Content-Length（即使值相同）",
            DUP_FIRST: "重复的 Content-Length 取第一个值",
            DUP_LAST: "重复的 Content-Length 取最后一个值",
        }[self.duplicate_content_length]
        conflict_text = {
            CONFLICT_REJECT: "Content-Length 与 Transfer-Encoding 并存时拒绝请求",
            CONFLICT_CHUNKED_PRIORITY: (
                "Content-Length 与 Transfer-Encoding 并存时忽略 Content-Length，"
                "按 chunked 解析"
            ),
        }[self.cl_te_conflict]
        codings = ", ".join(self.allowed_transfer_codings)
        return (
            f"模式 {self.name!r}：{dup_text}；{conflict_text}；"
            f"仅接受传输编码 [{codings}] 且 chunked 必须是最后一个编码；"
            "只接受 CRLF 换行，拒绝裸 LF/裸 CR 与 obs-fold 折叠行。"
        )


STRICT = Mode(name="strict")
CL_FIRST = Mode(name="cl-first", duplicate_content_length=DUP_FIRST)
CL_LAST = Mode(name="cl-last", duplicate_content_length=DUP_LAST)
TE_PRIORITY = Mode(name="te-priority", cl_te_conflict=CONFLICT_CHUNKED_PRIORITY)

DEFAULT_MODES: tuple[Mode, ...] = (STRICT, CL_FIRST, CL_LAST, TE_PRIORITY)

_MODES_BY_NAME = {m.name: m for m in DEFAULT_MODES}


def mode_by_name(name: str) -> Mode:
    """按名字查找预定义模式。"""
    return _MODES_BY_NAME[name]
