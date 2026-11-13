"""解析器行为测试：正常/畸形请求、分片不变性、模式差异、资源限制。"""

from __future__ import annotations

import unittest

from http_boundary import (
    CL_FIRST,
    CL_LAST,
    DEFAULT_MODES,
    Limits,
    ParseError,
    LimitExceeded,
    STRICT,
    TE_PRIORITY,
)

from tests.helpers import outcome_equal, run

GET = b"GET / HTTP/1.1\r\nHost: example\r\n\r\n"
POST_FIXED = b"POST /submit HTTP/1.1\r\nHost: example\r\nContent-Length: 5\r\n\r\nhello"
POST_CHUNKED = (
    b"POST /upload HTTP/1.1\r\nHost: example\r\nTransfer-Encoding: chunked\r\n\r\n"
    b"5;foo=bar\r\nhello\r\n6\r\n world\r\n0\r\nX-Trailer: done\r\n\r\n"
)
PIPELINE = POST_FIXED + POST_CHUNKED + GET


class BasicParsingTest(unittest.TestCase):
    def test_simple_get(self):
        events, error = run(GET)
        self.assertIsNone(error)
        self.assertEqual(len(events), 1)
        (event,) = events
        self.assertEqual(event.method, "GET")
        self.assertEqual(event.framing, "none")
        self.assertEqual(event.body_length, 0)
        self.assertEqual(event.next_offset, len(GET))

    def test_fixed_length_body(self):
        events, error = run(POST_FIXED)
        self.assertIsNone(error)
        (event,) = events
        self.assertEqual(event.framing, "fixed")
        body = POST_FIXED[event.body_start : event.body_end]
        self.assertEqual(body, b"hello")
        self.assertEqual(event.body_length, 5)
        self.assertEqual(event.next_offset, len(POST_FIXED))

    def test_chunked_with_extension_and_trailer(self):
        events, error = run(POST_CHUNKED)
        self.assertIsNone(error)
        (event,) = events
        self.assertEqual(event.framing, "chunked")
        self.assertEqual(event.body_length, 11)  # "hello" + " world"
        self.assertEqual(event.trailers, (("X-Trailer", "done"),))
        self.assertEqual(event.next_offset, len(POST_CHUNKED))

    def test_pipeline_three_requests(self):
        events, error = run(PIPELINE)
        self.assertIsNone(error)
        self.assertEqual([e.method for e in events], ["POST", "POST", "GET"])
        # 每个请求的起点应等于上一个请求的 next_offset，且整体覆盖全部输入
        self.assertEqual(events[0].next_offset, len(POST_FIXED))
        self.assertEqual(events[1].next_offset, len(POST_FIXED) + len(POST_CHUNKED))
        self.assertEqual(events[2].next_offset, len(PIPELINE))
        for prev, nxt in zip(events, events[1:]):
            self.assertTrue(PIPELINE[prev.next_offset:].startswith(nxt.method.encode()))

    def test_empty_chunked_body(self):
        data = b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
        events, error = run(data)
        self.assertIsNone(error)
        self.assertEqual(events[0].body_length, 0)
        self.assertEqual(events[0].next_offset, len(data))


class SplitInvarianceTest(unittest.TestCase):
    """同一份数据无论怎么分片，解析结果必须一致。"""

    def test_every_two_part_split(self):
        expected = run(PIPELINE)
        for cut in range(len(PIPELINE) + 1):
            with self.subTest(cut=cut):
                got = run(PIPELINE, chunks=[PIPELINE[:cut], PIPELINE[cut:]])
                self.assertTrue(outcome_equal(expected, got))

    def test_byte_by_byte(self):
        expected = run(PIPELINE)
        got = run(PIPELINE, chunks=[PIPELINE[i : i + 1] for i in range(len(PIPELINE))])
        self.assertTrue(outcome_equal(expected, got))

    def test_split_inside_chunk_size_and_crlf(self):
        # 专门切在 chunk-size 行中间、CR 与 LF 之间、chunk 数据中间
        for cut in (117, 118, 119, 120, 121, 122, 123, 124, 125, 126, 127):
            with self.subTest(cut=cut):
                expected = run(PIPELINE)
                got = run(PIPELINE, chunks=[PIPELINE[:cut], PIPELINE[cut:]])
                self.assertTrue(outcome_equal(expected, got))


class ModeRuleTest(unittest.TestCase):
    DUP_CL = (
        b"POST / HTTP/1.1\r\nHost: x\r\n"
        b"Content-Length: 6\r\nContent-Length: 5\r\n\r\n"
        b"12345GET / HTTP/1.1\r\nHost: x\r\n\r\n"
    )
    DUP_CL_SAME = (
        b"POST / HTTP/1.1\r\nHost: x\r\n"
        b"Content-Length: 5\r\nContent-Length: 5\r\n\r\nhello"
    )
    CL_AND_TE = (
        b"POST / HTTP/1.1\r\nHost: x\r\n"
        b"Content-Length: 4\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"5\r\nhello\r\n0\r\n\r\n"
    )

    def test_strict_rejects_duplicate_cl_even_when_identical(self):
        _, error = run(self.DUP_CL_SAME, STRICT)
        self.assertIsNotNone(error)
        self.assertIn("duplicate", error.message)
        self.assertEqual(error.fields, ("content-length",))

    def test_cl_first_uses_first_value(self):
        events, error = run(self.DUP_CL, CL_FIRST)
        self.assertIsNone(error)
        # 第一个值是 6：正文为 "12345G"，之后剩 "ET / ..." 不是合法请求
        self.assertEqual(events[0].body_length, 6)
        self.assertEqual(
            self.DUP_CL[events[0].body_start : events[0].body_end], b"12345G"
        )

    def test_cl_last_uses_last_value(self):
        events, error = run(self.DUP_CL, CL_LAST)
        self.assertIsNone(error)
        # 最后一个值是 5：正文为 "12345"，之后紧跟一个完整的 GET 请求
        self.assertEqual(events[0].body_length, 5)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1].method, "GET")
        self.assertEqual(events[1].next_offset, len(self.DUP_CL))

    def test_cl_first_and_last_disagree_on_boundaries(self):
        first_events, _ = run(self.DUP_CL, CL_FIRST)
        last_events, _ = run(self.DUP_CL, CL_LAST)
        self.assertNotEqual(first_events[0].body_end, last_events[0].body_end)
        self.assertNotEqual(first_events[0].next_offset, last_events[0].next_offset)

    def test_strict_rejects_cl_with_te(self):
        _, error = run(self.CL_AND_TE, STRICT)
        self.assertIsNotNone(error)
        self.assertEqual(error.fields, ("content-length", "transfer-encoding"))

    def test_te_priority_ignores_cl_and_uses_chunked(self):
        events, error = run(self.CL_AND_TE, TE_PRIORITY)
        self.assertIsNone(error)
        self.assertEqual(events[0].framing, "chunked")
        self.assertEqual(events[0].body_length, 5)  # 不是 CL 声明的 4
        self.assertEqual(events[0].next_offset, len(self.CL_AND_TE))

    def test_unsupported_transfer_coding_rejected_in_all_modes(self):
        data = b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: gzip\r\n\r\n"
        for mode in DEFAULT_MODES:
            with self.subTest(mode=mode.name):
                _, error = run(data, mode)
                self.assertIsNotNone(error)
                self.assertIn("unsupported transfer coding", error.message)

    def test_chunked_must_be_final_coding(self):
        data = (
            b"POST / HTTP/1.1\r\nHost: x\r\n"
            b"Transfer-Encoding: chunked, gzip\r\n\r\n"
        )
        _, error = run(data, STRICT)
        self.assertIsNotNone(error)
        self.assertIn("unsupported transfer coding", error.message)

    def test_invalid_content_length_value(self):
        data = b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 1x\r\n\r\n"
        _, error = run(data, STRICT)
        self.assertIsNotNone(error)
        self.assertIn("invalid Content-Length", error.message)


class MalformedTest(unittest.TestCase):
    def test_bare_lf_rejected(self):
        data = b"GET / HTTP/1.1\nHost: x\n\n"
        _, error = run(data)
        self.assertIsNotNone(error)
        self.assertIn("bare LF", error.message)

    def test_bare_cr_rejected(self):
        data = b"GET / HTTP/1.1\r\nHost: x\ry\r\n\r\n"
        _, error = run(data)
        self.assertIsNotNone(error)
        self.assertIn("bare CR", error.message)

    def test_obs_fold_rejected(self):
        data = b"GET / HTTP/1.1\r\nHost: x\r\n folded\r\n\r\n"
        _, error = run(data)
        self.assertIsNotNone(error)
        self.assertIn("folding", error.message)

    def test_bad_request_line(self):
        _, error = run(b"GET\r\n\r\n")
        self.assertIsNotNone(error)
        self.assertIn("request line", error.message)

    def test_header_without_colon(self):
        _, error = run(b"GET / HTTP/1.1\r\nHost x\r\n\r\n")
        self.assertIsNotNone(error)

    def test_invalid_chunk_size(self):
        data = b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n"
        _, error = run(data)
        self.assertIsNotNone(error)
        self.assertIn("chunk size", error.message)

    def test_missing_crlf_after_chunk_data(self):
        data = (
            b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"3\r\nabcXX\r\n0\r\n\r\n"
        )
        _, error = run(data)
        self.assertIsNotNone(error)
        self.assertIn("CRLF after chunk", error.message)

    def test_control_character_in_chunk_extension(self):
        data = (
            b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"3;bad=\x01\r\nabc\r\n0\r\n\r\n"
        )
        _, error = run(data)
        self.assertIsNotNone(error)
        self.assertIn("chunk extension", error.message)


class TruncationTest(unittest.TestCase):
    def test_truncated_mid_headers(self):
        _, error = run(POST_FIXED[:20])
        self.assertIsNotNone(error)
        self.assertIn("truncated", error.message)

    def test_truncated_mid_fixed_body(self):
        _, error = run(POST_FIXED[:-2])
        self.assertIsNotNone(error)
        self.assertIn("truncated", error.message)

    def test_truncated_mid_chunk(self):
        _, error = run(POST_CHUNKED[:-5])
        self.assertIsNotNone(error)
        self.assertIn("truncated", error.message)

    def test_truncated_reports_absolute_offset(self):
        cut = len(POST_FIXED) - 2
        _, error = run(POST_FIXED[:cut])
        self.assertEqual(error.offset, cut)


class LimitsTest(unittest.TestCase):
    SMALL = Limits(
        max_header_bytes=64,
        max_header_fields=4,
        max_body_bytes=16,
        max_chunks=3,
        max_line_bytes=32,
        max_buffer_bytes=64,
    )

    def test_header_bytes_limit(self):
        data = b"GET / HTTP/1.1\r\nHost: x\r\nX: " + b"y" * 100 + b"\r\n\r\n"
        _, error = run(data, limits=self.SMALL)
        self.assertIsInstance(error, LimitExceeded)
        self.assertIn("header block", error.message)

    def test_header_fields_limit(self):
        fields = b"".join(b"X-%d: 1\r\n" % i for i in range(10))
        data = b"GET / HTTP/1.1\r\n" + fields + b"\r\n"
        _, error = run(data, limits=self.SMALL)
        self.assertIsInstance(error, LimitExceeded)
        self.assertIn("header fields", error.message)

    def test_huge_declared_content_length_fails_fast(self):
        # 巨大的声明长度应在头部解析完成时立即拒绝，而不是等待数据
        data = b"POST / HTTP/1.1\r\nContent-Length: 999999999\r\n\r\n"
        events, error = run(data, limits=self.SMALL)
        self.assertIsInstance(error, LimitExceeded)
        self.assertIn("Content-Length", error.message)
        self.assertEqual(events, [])

    def test_chunk_count_limit(self):
        data = (
            b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n"
            + b"1\r\na\r\n" * 10
            + b"0\r\n\r\n"
        )
        _, error = run(data, limits=self.SMALL)
        self.assertIsInstance(error, LimitExceeded)
        self.assertIn("chunks", error.message)

    def test_chunked_body_size_limit(self):
        data = (
            b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"11\r\n" + b"a" * 17 + b"\r\n0\r\n\r\n"
        )
        _, error = run(data, limits=self.SMALL)
        self.assertIsInstance(error, LimitExceeded)
        self.assertIn("chunked body", error.message)

    def test_line_length_limit(self):
        data = b"GET /" + b"x" * 100 + b" HTTP/1.1\r\n\r\n"
        _, error = run(data, limits=self.SMALL)
        self.assertIsInstance(error, LimitExceeded)

    def test_persistent_fragments_terminate(self):
        # 持续喂入永远不成行的碎片：必须在缓冲上限处安全结束
        from http_boundary import RequestParser

        parser = RequestParser(STRICT, self.SMALL)
        with self.assertRaises(LimitExceeded):
            for _ in range(10_000):
                parser.feed(b"x")

    def test_fragmented_valid_request_completes(self):
        # 逐字节喂入一个合法流水线，应正常完成且结果一致
        expected = run(PIPELINE)
        got = run(
            PIPELINE,
            limits=Limits(),
            chunks=[PIPELINE[i : i + 1] for i in range(len(PIPELINE))],
        )
        self.assertTrue(outcome_equal(expected, got))


if __name__ == "__main__":
    unittest.main()
