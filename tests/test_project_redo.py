import unittest
import test_core
from gbm.core import Project, GBMError

class ProjectRedoTests(unittest.TestCase):
    setUp = test_core.HistoryTests.setUp
    write = test_core.HistoryTests.write
    capture = test_core.HistoryTests.capture
    restore = test_core.HistoryTests.restore

    def states(self):
        self.write('a','one'); self.write('deleted','recover me'); self.capture()
        self.write('a','two'); (self.root/'deleted').unlink(); self.write('added','new'); self.capture()

    def test_add_modify_delete_redo_and_persistence(self):
        self.states(); original=self.p.scan(); self.restore()
        self.assertTrue((self.root/'deleted').exists())
        self.assertFalse((self.root/'added').exists())
        self.p=Project(self.root)
        self.restore(redo=True)
        self.assertEqual(self.p.scan(),original)
        with self.assertRaises(GBMError): self.p.preview(redo=True)

    def test_multiple_undo_redo(self):
        for value in ['one','two','three']:
            self.write('a',value); self.capture()
        self.restore(); self.restore()
        self.restore(redo=True); self.assertEqual((self.root/'a').read_text(),'two')
        self.restore(redo=True); self.assertEqual((self.root/'a').read_text(),'three')
        self.restore(); self.assertEqual((self.root/'a').read_text(),'two')

    def test_new_edit_invalidates_redo_even_before_watcher(self):
        self.states(); self.restore(); self.write('a','new work')
        with self.assertRaises(GBMError): self.p.preview(redo=True)
        self.assertEqual((self.root/'a').read_text(),'new work')

    def test_file_restore_invalidates_project_redo(self):
        for value in ['one','two','three']:
            self.write('a',value); self.capture()
        self.restore(); self.restore('a')
        with self.assertRaises(GBMError): self.p.preview(redo=True)
        self.restore('a',True); self.assertEqual((self.root/'a').read_text(),'two')

    def test_no_redo(self):
        self.states()
        with self.assertRaisesRegex(GBMError,'No eligible project redo'):
            self.p.preview(redo=True)

    def test_conflict_keeps_redo_unconsumed(self):
        self.states(); self.restore(); plan=self.p.preview(redo=True)
        stack=self.p.get('project_redo'); self.write('a','concurrent edit')
        with self.assertRaises(GBMError): self.p.apply(*plan)
        self.assertEqual(self.p.get('project_redo'),stack)
        self.assertEqual((self.root/'a').read_text(),'concurrent edit')

    def test_interrupted_redo_recovery(self):
        self.states(); self.restore(); before=self.p.scan()
        plan=self.p.preview(redo=True); original=self.p.write_state
        def fail(*args):
            self.write('a','partial'); raise OSError('interrupted')
        self.p.write_state=fail
        with self.assertRaises(OSError): self.p.apply(*plan)
        with self.assertRaises(GBMError): self.p.preview(redo=True)
        self.p.write_state=original; self.p.recover()
        self.assertEqual(self.p.scan(),before)
        self.assertEqual(self.p.get('project_redo'),[])

    def test_history_conflict_without_content_change(self):
        self.states(); self.restore(); plan=self.p.preview(redo=True)
        self.p.set('project_redo', [])
        with self.assertRaisesRegex(GBMError,'history changed'):
            self.p.apply(*plan)
        self.assertEqual((self.root/'a').read_text(),'one')
