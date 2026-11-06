"""키움 OCX 없이 선물옵션 잔고 파싱을 확인한다."""

import ast
import logging
import math
import re
import unittest
from datetime import datetime
from pathlib import Path


def daemon_class():
    # Windows 의존성과 모듈 초기화(.env·로그)를 실행하지 않고 데몬 클래스만 읽는다.
    path = Path(__file__).resolve().parents[1] / "kiwoom_daemon.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "KiwoomDaemon")
    scope = {"datetime": datetime, "log": logging.getLogger(__name__), "math": math, "re": re}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), scope)
    return scope["KiwoomDaemon"]


def balance(net, deposit="10000", rows=()):
    cls = daemon_class()
    daemon = cls.__new__(cls)
    daemon.account = "synthetic"
    replies = {"OPW20010": {"순자산금액": net, "예탁총액": deposit}, "OPW20006": rows}
    daemon._tr_request = lambda _rq, tr, *_: replies[tr]
    return daemon._futures_balance()


class BalanceTests(unittest.TestCase):
    def test_zero_net_does_not_fall_back_to_deposit(self):
        for raw in ("0000", "-0000", "+0.0"):
            with self.subTest(raw=raw):
                got = balance(raw)
                self.assertEqual((got["net_asset_krw"], got["total_krw"]), (0, 0))

    def test_unknown_net_stays_null_with_legacy_deposit_fallback(self):
        for raw in ("", "N/A", "--", "NaN", "Infinity", "1+2", "1,,2"):
            with self.subTest(raw=raw):
                got = balance(raw, deposit="+10,000")
                self.assertIsNone(got["net_asset_krw"])
                self.assertEqual(got["total_krw"], 10000)

    def test_unknown_net_and_deposit_do_not_return_zero(self):
        with self.assertRaisesRegex(RuntimeError, "OPW20010 빈 응답"):
            balance("N/A", deposit="--")

    def test_missing_or_bad_qty_marks_positions_incomplete(self):
        for raw in ("", "N/A", "--", "NaN", "Infinity", "1+2"):
            with self.subTest(raw=raw):
                got = balance("9800", rows=[{"종목코드": "SYN1", "잔고수량": raw},
                                           {"종목코드": "SYN2", "잔고수량": "+1,234"}])
                self.assertFalse(got["positions_ok"])
                self.assertEqual(got["positions_error"], "잔고수량 확인 불가")
                self.assertEqual([row["qty"] for row in got["positions"]], [1234])
                self.assertEqual(got["net_asset_krw"], 9800)

    def test_zero_qty_and_empty_placeholder_are_known_empty(self):
        got = balance("-1,000.25", rows=[{}, {"종목코드": "SYN", "잔고수량": "0000"}])
        self.assertTrue(got["positions_ok"])
        self.assertEqual(got["positions"], [])
        self.assertEqual(got["net_asset_krw"], -1000.25)

    def test_nonzero_qty_with_missing_code_is_incomplete(self):
        got = balance("9800", rows=[{"잔고수량": "1"}])
        self.assertFalse(got["positions_ok"])
        self.assertEqual(got["positions"], [])


if __name__ == "__main__":
    unittest.main()
