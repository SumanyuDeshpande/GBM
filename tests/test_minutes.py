import contextlib
import io
import math
from unittest.mock import patch
import unittest
import test_core
from gbm.core import GBMError
from gbm.cli import main

class TimedUndoTests(unittest.TestCase):
    setUp = test_core.HistoryTests.setUp
    write = test_core.HistoryTests.write
    capture = test_core.HistoryTests.capture
    restore = test_core.HistoryTests.restore
    def timeline(self):
        with patch('gbm.core.time.time', return_value=1000):
            self.write('a','one'); self.write('other','original'); self.capture()
        with patch('gbm.core.time.time', return_value=1120):
            self.write('a','two'); self.capture()
        with patch('gbm.core.time.time', return_value=1240):
            self.write('a','three'); self.write('other','new'); self.capture()
    def plan(self, minutes):
        with patch('gbm.core.time.time', return_value=1300):
            return self.p.preview('a', minutes=minutes)
    def test_jump_and_single_redo(self):
        self.timeline()
        self.p.apply(*self.plan(3))
        self.assertEqual((self.root/'a').read_text(),'two')
        self.assertEqual((self.root/'other').read_text(),'new')
        self.restore('a',True)
        self.assertEqual((self.root/'a').read_text(),'three')
    def test_fractional_minutes(self):
        self.timeline(); self.p.apply(*self.plan(1.5))
        self.assertEqual((self.root/'a').read_text(),'two')
    def test_exact_boundary(self):
        self.timeline(); self.p.apply(*self.plan(3))
        self.assertEqual(self.p.get('file_head:a'),2)
        self.restore('a'); self.assertEqual((self.root/'a').read_text(),'one')
    def test_no_old_enough_checkpoint(self):
        self.timeline()
        with self.assertRaisesRegex(GBMError,'No checkpoint'):
            self.plan(6)
        self.assertEqual((self.root/'a').read_text(),'three')
    def test_absent_file_and_redo(self):
        with patch('gbm.core.time.time',return_value=1000): self.capture()
        with patch('gbm.core.time.time',return_value=1240):
            self.write('a','new'); self.capture()
        self.p.apply(*self.plan(3))
        self.assertFalse((self.root/'a').exists())
        self.restore('a',True); self.assertEqual((self.root/'a').read_text(),'new')
    def test_invalid_minutes(self):
        self.timeline()
        for minutes in [0,-1,math.nan,math.inf,-math.inf]:
            with self.subTest(minutes=minutes), self.assertRaises(GBMError):
                self.plan(minutes)
        with self.assertRaises(GBMError): self.p.preview(minutes=5)
    def test_jump_conflict(self):
        self.timeline(); plan=self.plan(3); self.write('a','new work')
        with self.assertRaises(GBMError): self.p.apply(*plan)
        self.assertEqual((self.root/'a').read_text(),'new work')
    def test_cli_requires_file(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(['undo','--minutes','5'])
        self.assertEqual(caught.exception.code,2)
