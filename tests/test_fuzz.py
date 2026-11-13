"""固定随机种子的变异测试。

核心性质：同一模式下，同一份数据的解析结果与分片方式无关。
发现反例时把输入保存到 tests/fuzz_failures/ 并让测试失败；
ReplayTest 会重新加载这些用例，确认问题可复现（或已修复）。
"""

from __future__ import annotations

import json
import random
import unittest

from http_boundary import DEFAULT_MODES, mode_by_name

from tests.helpers import (
    FAILURE_DIR,
    outcome_equal,
    run,
    save_failure_case,
)

SEED = 20261113
ITERATIONS = 300
SPLITS_PER_INPUT = 4

CORPUS = [
    b"GET / HTTP/1.1\r\nHost: example\r\n\r\n",
    b"POST /s HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello",
    (
        b"POST /u HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"5;ext=1\r\nhello\r\n6\r\n world\r\n0\r\nX-T: y\r\n\r\n"
    ),
    (
        b"POST /d HTTP/1.1\r\nHost: x\r\n"
        b"Content-Length: 6\r\nContent-Length: 5\r\n\r\n12345GET / HTTP/1.1\r\n\r\n"
    ),
    (
        b"POST /c HTTP/1.1\r\nHost: x\r\n"
        b"Content-Length: 4\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"5\r\nhello\r\n0\r\n\r\n"
    ),
    b"GET /a HTTP/1.1\r\nHost: x\r\n\r\nGET /b HTTP/1.1\r\nHost: y\r\n\r\n",
    b"not http at all",
    b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 10\r\n\r\nshort",
]


def mutate(rng: random.Random, data: bytes) -> bytes:
    """对输入做 1-4 次随机小变异（改字节、插入、删除、插 CRLF、插 CL 头）。"""
    buf = bytearray(data)
    for _ in range(rng.randint(1, 4)):
        if not buf:
            break
        pos = rng.randrange(len(buf))
        op = rng.randrange(6)
        if op == 0:
            buf[pos] = rng.randrange(256)
        elif op == 1:
            buf[pos:pos] = bytes([rng.randrange(256)])
        elif op == 2:
            del buf[pos]
        elif op == 3:
            buf[pos:pos] = b"\r\n"
        elif op == 4:
            buf[pos:pos] = b"Content-Length: " + str(rng.randrange(20)).encode() + b"\r\n"
        else:
            buf[pos:pos] = bytes([buf[pos]]) * rng.randrange(2, 5)
    return bytes(buf)


def random_chunks(rng: random.Random, data: bytes) -> list[bytes]:
    """把数据随机切成若干片（含空片）。"""
    if not data:
        return [b""]
    cuts = sorted(rng.randrange(len(data) + 1) for _ in range(rng.randint(1, 8)))
    chunks, prev = [], 0
    for cut in cuts:
        chunks.append(data[prev:cut])
        prev = cut
    chunks.append(data[prev:])
    return chunks


class ChunkingInvarianceFuzzTest(unittest.TestCase):
    def test_chunking_does_not_change_results(self):
        rng = random.Random(SEED)
        for iteration in range(ITERATIONS):
            data = mutate(rng, rng.choice(CORPUS))
            for mode in DEFAULT_MODES:
                expected = run(data, mode)
                for _ in range(SPLITS_PER_INPUT):
                    chunks = random_chunks(rng, data)
                    got = run(data, mode, chunks=chunks)
                    if not outcome_equal(expected, got):
                        path = save_failure_case(mode, data, chunks)
                        self.fail(
                            f"分片改变了解析结果：iteration={iteration} "
                            f"mode={mode.name}，用例已保存到 {path}"
                        )


class ReplayTest(unittest.TestCase):
    """重放 tests/fuzz_failures/ 中保存的失败用例。"""

    def test_replay_saved_cases(self):
        files = sorted(FAILURE_DIR.glob("*.json")) if FAILURE_DIR.is_dir() else []
        if not files:
            self.skipTest("没有保存的失败用例")
        for path in files:
            with self.subTest(case=path.name):
                payload = json.loads(path.read_text())
                data = bytes.fromhex(payload["data"])
                chunks = [bytes.fromhex(p) for p in payload["chunks"]]
                mode = mode_by_name(payload["mode"])
                expected = run(data, mode)
                got = run(data, mode, chunks=chunks)
                self.assertTrue(
                    outcome_equal(expected, got),
                    f"重放用例 {path.name} 仍然复现分片不一致",
                )


if __name__ == "__main__":
    unittest.main()
