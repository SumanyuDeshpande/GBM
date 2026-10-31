import json
import tempfile
import unittest
from pathlib import Path
from gbm.core import Project, GBMError

class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.p = Project(self.root)
        self.p.initialize()
    def write(self, name, text):
        (self.root / name).write_text(text)
    def capture(self):
        return self.p.capture()
    def restore(self, rel=None, redo=False):
        self.p.apply(*self.p.preview(rel, redo))
    def test_file_undo_redo_multiple_levels(self):
        self.write('a', 'one'); self.capture()
        self.write('a', 'two'); self.capture()
        self.write('a', 'three'); self.write('b','untouched'); self.capture()
        self.restore('a'); self.assertEqual((self.root/'a').read_text(), 'two')
        self.restore('a'); self.assertEqual((self.root/'a').read_text(), 'one')
        self.restore('a', True); self.assertEqual((self.root/'a').read_text(), 'two')
        self.restore('a', True); self.assertEqual((self.root/'a').read_text(), 'three')
        self.assertEqual((self.root/'b').read_text(), 'untouched')
    def test_project_add_delete_and_repeated_undo(self):
        self.write('a','one'); self.capture()
        (self.root/'a').unlink(); self.write('b','two'); self.capture()
        self.write('b','three'); self.capture()
        self.restore(); self.assertEqual((self.root/'b').read_text(),'two')
        self.restore(); self.assertTrue((self.root/'a').exists()); self.assertFalse((self.root/'b').exists())
    def test_unsaved_checkpoint_changes_preserved(self):
        self.write('a','one'); self.capture()
        self.write('a','two'); self.restore('a')
        self.restore('a',True); self.assertEqual((self.root/'a').read_text(),'two')
    def test_conflict(self):
        self.write('a','one'); self.capture()
        self.write('a','two'); self.capture()
        plan=self.p.preview('a'); self.write('a','new work')
        with self.assertRaises(GBMError): self.p.apply(*plan)
        self.assertEqual((self.root/'a').read_text(),'new work')
    def test_interrupt_and_recovery(self):
        self.write('a','one'); self.capture()
        self.write('a','two'); self.capture()
        plan=self.p.preview('a')
        original=self.p.write_state
        def fail(before,target):
            self.write('a','partial')
            raise OSError('interrupted')
        self.p.write_state=fail
        with self.assertRaises(OSError): self.p.apply(*plan)
        with self.assertRaises(GBMError): self.capture()
        self.p.write_state=original; self.p.recover()
        self.assertEqual((self.root/'a').read_text(),'two')
        self.assertFalse(self.p.pending())
    def test_exclusions_and_boundaries(self):
        self.write('.env','secret'); self.write('a','code'); self.capture()
        self.assertEqual(set(self.p.scan()), {'a'})
        for name in ['../outside', '.gbm/config.json', '.env']:
            with self.assertRaises(GBMError): self.p.path(name)
    def test_new_edit_invalidates_redo(self):
        self.write('a','one'); self.capture()
        self.write('a','two'); self.capture(); self.restore('a')
        self.write('a','three'); self.capture()
        with self.assertRaises(GBMError): self.p.preview('a',True)
    def test_identical_state_deduplicated(self):
        self.write('a','one'); self.capture()
        self.assertIsNone(self.capture())
        self.assertEqual(len(list((self.p.home/'objects').iterdir())),1)
    def test_corrupt_object_never_overwrites(self):
        self.write('a','one'); self.capture()
        self.write('a','two'); self.capture()
        plan=self.p.preview('a')
        (self.p.home/'objects'/plan[1]['a']).write_bytes(b'broken')
        with self.assertRaises(GBMError): self.p.apply(*plan)
        self.assertEqual((self.root/'a').read_text(),'two')

if __name__=='__main__': unittest.main()
