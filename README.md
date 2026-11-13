# http-boundary-analyzer

HTTP/1.1 请求边界对照分析库（纯 Python 3.11+ 标准库，离线运行）。

同一段原始请求字节流，经过不同的解析规则（"模式"）处理后，可能得到不同
的消息边界——这是 HTTP 请求走私类问题的成因之一。本库用多个明确定义的
假想解析模式独立解析同一份输入，比较它们**接受/拒绝的结果、每个请求的
正文范围、下一个请求的起点**，并在发现分歧时给出原始数据中的偏移、相关
头部字段和产生差异的规则原因。

**性质与边界（请务必阅读）**

- 这是**离线静态模拟**：不连接任何真实服务器，不发送网络流量。
- 各模式是**假想的对照解析器**，不对应任何真实服务器产品；真实中间设备
  的行为可能更宽松（如容忍裸 LF、obs-fold）或更怪异，本库未一一模拟。
- 分歧报告说明"这些规则下边界会分叉"，**不能**证明某种走私在真实环境中
  一定可利用，也**不能**保证发现所有请求走私问题。
- 只解析 HTTP/1.0 / HTTP/1.1 的**请求**；不涉及响应、HTTP/2、WebSocket。

## 目录结构

```
http_boundary/
  parser.py     # 增量式请求解析器（状态机，分片无关）
  modes.py      # 解析模式定义（严格模式 + 3 个兼容模式）
  analyzer.py   # 跨模式对照分析与分歧报告
  limits.py     # 资源上限配置
  errors.py     # ParseError / LimitExceeded
tests/
  test_parser.py      # 解析器行为测试
  test_divergence.py  # 分歧报告测试
  test_fuzz.py        # 固定种子变异测试（分片不变性）+ 失败用例重放
  fuzz_failures/      # 变异测试失败时自动保存的用例（可重放）
```

## 快速上手

```python
from http_boundary import analyze, run_mode, STRICT, CL_FIRST, CL_LAST, TE_PRIORITY

data = (
    b"POST / HTTP/1.1\r\nHost: x\r\n"
    b"Content-Length: 6\r\nContent-Length: 5\r\n\r\n"
    b"12345GET / HTTP/1.1\r\nHost: x\r\n\r\n"
)

report = analyze(data)  # 默认用全部 4 个模式两两比较
print(report.consistent)        # False：存在分歧
for div in report.divergences:
    print(div.kind, div.offset, div.fields)
    print(div.reason)           # 中文说明：规则差异 + 数据特征
    print(div.details)          # 每个模式各自的解析结果描述

# 单模式增量解析
from http_boundary import RequestParser
parser = RequestParser(STRICT)
events = parser.feed(data[:30])   # 可任意分片多次喂入
events += parser.feed(data[30:])
events += parser.finish()         # 输入结束；截断会抛 ParseError
```

## API 概览

### `RequestParser(mode=STRICT, limits=Limits())`

增量解析器。`feed(bytes) -> list[RequestEvent]` 可多次调用；
`finish()` 声明输入结束，若正处于请求中途则抛出 `ParseError`（截断）。
`feed()` 抛出异常时，本次调用中已完成的请求事件仍保留在解析器内，可用
`drain_events()` 取回。同一模式下，解析结果与分片方式无关。

### `RequestEvent`

一个完整请求的解析结果，偏移均为整个输入流的绝对偏移：

| 字段 | 含义 |
| --- | --- |
| `method` / `target` / `version` | 请求行三部分 |
| `headers` / `trailers` | `(名字, 值)` 元组，保持原始顺序 |
| `framing` | `"none"` / `"fixed"` / `"chunked"` |
| `body_start` / `body_end` | 正文区段 `[start, end)`；chunked 从第一个 chunk-size 行到 trailer 结束 |
| `body_length` | 解码后正文长度（chunked 为各块之和） |
| `next_offset` | 下一个请求的起点 |

### `analyze(data, modes=DEFAULT_MODES, limits=None) -> AnalysisReport`

用每个模式独立解析同一份数据并两两比较。`AnalysisReport.outcomes` 是各
模式的完整结果（`ModeOutcome.requests` + `ModeOutcome.error`）；
`AnalysisReport.divergences` 是分歧列表，每条 `Divergence` 包含：

- `kind`：`accept-reject`（一方接受一方拒绝）、`boundary`（正文范围或下一
  请求起点不同）、`request-count`（请求数量不同）、`error-detail`（双方都
  拒绝但错误不同）。
- `offset`：分歧在原始数据中的绝对偏移。
- `fields`：相关头部字段（如 `content-length`）。
- `reason`：规则差异与数据特征的中文说明。
- `details`：每个模式各自的结果描述。

### 错误分类

- **数据未收齐**：不是错误。`feed()` 返回已完成的请求，其余等待更多输入。
- **格式错误 `ParseError`**：含 `message`、`offset`、`fields`。
  输入结束时数据不完整也属于此类（`"truncated input"`）。
- **资源超限 `LimitExceeded`**（`ParseError` 的子类）：超过 `Limits` 上限。

## 解析模式规则

所有模式共享的基础规则：只接受 CRLF 换行（拒绝裸 LF / 裸 CR）；拒绝
obs-fold 折叠行；`Transfer-Encoding` 只接受 `chunked` 且必须是最后一个
编码；`Content-Length` 必须是纯十进制数字。各模式只在下表两条规则上不同：

| 模式 | 重复 Content-Length | CL 与 TE 并存 |
| --- | --- | --- |
| `strict` | 拒绝（即使值相同） | 拒绝 |
| `cl-first` | 取第一个值 | 拒绝 |
| `cl-last` | 取最后一个值 | 拒绝 |
| `te-priority` | 拒绝 | 忽略 CL，按 chunked 解析 |

这些模式用于对照分析，**不对应任何真实服务器产品**。可以用
`Mode(...)` 自定义规则组合。

## 资源限制

`Limits` 的默认值（均可调）：

| 上限 | 默认值 | 说明 |
| --- | --- | --- |
| `max_header_bytes` | 16 KiB | 请求行 + 头部块 |
| `max_header_fields` | 100 | 头部 / trailer 字段数 |
| `max_body_bytes` | 8 MiB | 单请求正文（chunked 按解码后累计） |
| `max_chunks` | 10000 | 单请求 chunk 数 |
| `max_line_bytes` | 8 KiB | 任意一行 |
| `max_buffer_bytes` | 1 MiB | 解析器内部未消费缓冲 |

巨大的声明长度在头部解析完成时立即拒绝，不会等待数据到齐；持续喂入
永远不成行的碎片会在行长/缓冲上限处安全终止。已消费的数据不会被重复
扫描（行搜索从上次失败位置继续），逐字节喂入也不会退化。

## 运行测试

```bash
cd <仓库根目录>
python3 -m unittest discover -v
```

测试覆盖：多请求连续输入（流水线）、逐字节/任意切点的分片不变性、重复
Content-Length（相同/冲突值）、CL 与 TE 冲突、chunk 扩展与 trailer、块
边界切分、截断输入、各项资源上限、持续碎片输入终止、跨模式分歧报告的
偏移/字段/原因，以及固定种子（`SEED = 20261113`）的变异测试——验证同一
模式下分片方式不改变解析结果。变异测试发现反例时会把输入保存到
`tests/fuzz_failures/` 并让测试失败；`ReplayTest` 会重放这些用例。

## 实测结果与已知未覆盖项

在 Python 3.14 上运行：`Ran 46 tests ... OK`（含 300 组变异输入 × 4 模式
× 4 种随机分片的不变性检查；另用不同种子手动跑过 5000 组，无分歧）。
开发过程中变异测试曾发现一处真实缺陷（`feed()` 抛错时丢失已完成事件），
已修复，触发用例保存在 `tests/fuzz_failures/` 作为回归用例。

已知未覆盖 / 有意不做的部分：

- 不模拟容忍裸 LF、obs-fold、重复 TE、非法字符忽略等更宽松的兼容行为。
- 不解码 `gzip` 等非 chunked 传输编码（直接拒绝）。
- 不处理响应、HEAD/204 等无正文语义、CONNECT、HTTP/1.0 keep-alive 细节。
- 变异测试是固定种子的随机抽样，不是穷尽验证。
- 分歧结论只反映本库定义的规则组合，不能外推到真实部署的所有组件。
