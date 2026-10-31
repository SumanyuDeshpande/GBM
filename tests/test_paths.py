import ctypes
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from gbm.core import Project, GBMError

class PathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project with spaces'
        self.root.mkdir()
        self.p = Project(self.root)
        self.p.initialize()
        (self.root / 'a.py').write_text('source')

    @unittest.skipUnless(os.name == 'nt', 'Windows short paths')
    def test_short_path_alias(self):
        function = ctypes.windll.kernel32.GetShortPathNameW
        function.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        function.restype = ctypes.c_uint
        buffer = ctypes.create_unicode_buffer(32768)
        size = function(str(self.root), buffer, len(buffer))
        self.assertTrue(size)
        short = Path(buffer.value)
        if short == self.root:
            self.skipTest('Filesystem does not supply a distinct short-name alias')
        # Relative and absolute arguments, including a deleted target.
        with patch('gbm.core.Path.cwd', return_value=short):
            self.assertEqual(self.p.file_arg('a.py'), 'a.py')
            self.assertEqual(self.p.file_arg('deleted.py'), 'deleted.py')
        self.assertEqual(self.p.file_arg(str(short/'a.py')), 'a.py')

    def test_outside_file_rejected(self):
        with self.assertRaises(GBMError):
            self.p.file_arg(str(self.root.parent/'outside.py'))

    def test_symlink_inside_project_rejected(self):
        link = self.root/'linked.py'
        try:
            link.symlink_to(self.root/'a.py')
        except OSError:
            self.skipTest('Symlink creation unavailable')
        with self.assertRaises(GBMError):
            self.p.file_arg(str(link))
