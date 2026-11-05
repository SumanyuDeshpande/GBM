"""Test process used by the agent, invoked with an isolated interpreter."""
import json
from pathlib import Path
import sys
import traceback


def main():
    root = Path(sys.argv[1])
    runner, path, report = sys.argv[2], sys.argv[3], Path(sys.argv[4])
    sys.path[:0] = [str(root), str(root/'src')]
    try:
        if runner == 'unittest':
            import unittest
            suite = unittest.defaultTestLoader.discover(str(root/path))
            count = suite.countTestCases()
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            status = 'PASS' if count and result.wasSuccessful() else 'FAIL' if count else 'INCONCLUSIVE'
            data = {'status': status, 'tests': count, 'failures': len(result.failures),
                    'errors': len(result.errors), 'skipped': len(result.skipped)}
            if count and count == len(result.skipped):
                data['status'] = 'INCONCLUSIVE'
        elif runner == 'pytest':
            import pytest
            class Count:
                total = 0
                def pytest_collection_finish(self, session):
                    self.total = session.testscollected
            plugin = Count()
            code = pytest.main([str(root/path), '-q', '--disable-warnings'], plugins=[plugin])
            data = {'status': 'PASS' if code == 0 and plugin.total else 'INCONCLUSIVE' if code == 5 else 'FAIL',
                    'tests': plugin.total, 'exit_code': int(code)}
        else:
            raise ValueError('Unsupported test runner')
    except BaseException as error:
        traceback.print_exc()
        data = {'status': 'INCONCLUSIVE', 'tests': 0, 'message': str(error)[:1000]}
    report.write_text(json.dumps(data, ensure_ascii=True), encoding='utf-8')
    return 0 if data['status'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
