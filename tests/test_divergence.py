"""跨模式对照分析测试：分歧报告必须包含偏移、字段和原因。"""

from __future__ import annotations

import unittest

from http_boundary import CL_FIRST, CL_LAST, STRICT, TE_PRIORITY, analyze

DUP_CL = (
    b"POST / HTTP/1.1\r\nHost: x\r\n"
    b"Content-Length: 6\r\nContent-Length: 5\r\n\r\n"
    b"12345GET / HTTP/1.1\r\nHost: x\r\n\r\n"
)
CL_AND_TE = (
    b"POST / HTTP/1.1\r\nHost: x\r\n"
    b"Content-Length: 4\r\nTransfer-Encoding: chunked\r\n\r\n"
    b"5\r\nhello\r\n0\r\n\r\n"
)
CLEAN = b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello"
GARBAGE = b"this is not http at all"


class DivergenceReportTest(unittest.TestCase):
    def test_duplicate_cl_accept_reject_and_boundary(self):
        report = analyze(DUP_CL)
        self.assertFalse(report.consistent)
        # strict 拒绝，cl-first/cl-last 接受 -> accept-reject
        accept_reject = [d for d in report.divergences if d.kind == "accept-reject"]
        self.assertTrue(accept_reject)
        for div in accept_reject:
            self.assertIn("content-length", div.fields)
            self.assertGreater(div.offset, 0)
            self.assertIn("Content-Length", div.reason)
        # cl-first 与 cl-last 都接受但边界不同 -> boundary
        boundary = [d for d in report.divergences if d.kind == "boundary"]
        self.assertEqual(len(boundary), 1)
        div = boundary[0]
        self.assertEqual(set(div.details), {"cl-first", "cl-last"})
        self.assertIn("content-length", div.fields)
        self.assertIn("边界", div.reason)
        self.assertTrue(div.reason)

    def test_boundary_divergence_points_into_raw_data(self):
        report = analyze(DUP_CL, modes=(CL_FIRST, CL_LAST))
        (div,) = report.divergences
        # 分歧偏移应落在第一个请求的正文区域起点
        self.assertEqual(div.offset, DUP_CL.index(b"12345"))

    def test_cl_te_conflict_accept_reject(self):
        report = analyze(CL_AND_TE, modes=(STRICT, TE_PRIORITY))
        self.assertFalse(report.consistent)
        (div,) = report.divergences
        self.assertEqual(div.kind, "accept-reject")
        self.assertEqual(div.fields, ("content-length", "transfer-encoding"))
        self.assertIn("chunked", div.reason)
        # te-priority 接受且按 chunked 解析
        outcome = report.outcomes["te-priority"]
        self.assertIsNone(outcome.error)
        self.assertEqual(outcome.requests[0].framing, "chunked")
        self.assertEqual(outcome.requests[0].body_length, 5)

    def test_clean_input_is_consistent(self):
        report = analyze(CLEAN)
        self.assertTrue(report.consistent)
        self.assertEqual(report.divergences, ())
        for outcome in report.outcomes.values():
            self.assertIsNone(outcome.error)
            self.assertEqual(len(outcome.requests), 1)

    def test_garbage_rejected_identically_is_consistent(self):
        report = analyze(GARBAGE)
        self.assertTrue(report.consistent)
        for outcome in report.outcomes.values():
            self.assertIsNotNone(outcome.error)

    def test_smuggled_request_interpretation(self):
        # cl-first 取 6：正文吞掉 "12345G"，剩余字节被解析成方法为 "ET" 的请求；
        # cl-last 取 5：剩余字节是正常的 "GET" 请求。两边都"成功"但内容不同。
        report = analyze(DUP_CL, modes=(CL_FIRST, CL_LAST))
        kinds = {d.kind for d in report.divergences}
        self.assertIn("boundary", kinds)
        first = report.outcomes["cl-first"]
        last = report.outcomes["cl-last"]
        self.assertEqual(len(first.requests), 2)
        self.assertEqual(len(last.requests), 2)
        self.assertEqual(first.requests[1].method, "ET")
        self.assertEqual(last.requests[1].method, "GET")

    def test_reason_mentions_rule_difference(self):
        report = analyze(DUP_CL, modes=(STRICT, CL_LAST))
        (div,) = report.divergences
        self.assertIn("取最后一个值", div.reason)
        self.assertIn("拒绝", div.reason)


if __name__ == "__main__":
    unittest.main()
