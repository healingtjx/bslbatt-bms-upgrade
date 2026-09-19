"""Exercise remote shell commands locally without SSH or CAN hardware."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import pc_update_remote as remote


class RemoteTests(unittest.TestCase):
    def test_upload_backup_and_remote_exit(self):
        with tempfile.TemporaryDirectory(prefix="remote ' space ") as directory:
            root = Path(directory)
            target = root / 'pc_update.py'
            target.write_text('old script')
            (root / 'P41288V110-41289-1.52T-000.bin').write_bytes(b'firmware')
            source = b"print('test result', flush=True)\nraise SystemExit(4)\n"
            result = subprocess.run(['sh', '-c', remote.upload_command(directory)],
                                    input=source, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(target.read_bytes(), source)
            backups = list(root.glob('*.bak'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), 'old script')
            result = subprocess.run(['sh', '-c', remote.run_command(directory)], capture_output=True)
            self.assertEqual(result.returncode, 4, result.stderr)
            self.assertIn(b'test result', result.stdout)

    def test_invalid_upload_preserves_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'pc_update.py'
            target.write_text('old script')
            command = ['sh', '-c', remote.upload_command(directory)]
            # Missing firmware prevents replacement.
            result = subprocess.run(command, input=b'print(1)', capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(target.read_text(), 'old script')
            (root / 'P41288V110-41289-1.52T-000.bin').write_bytes(b'firmware')
            result = subprocess.run(command, input=b'def invalid(', capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(target.read_text(), 'old script')
            self.assertEqual(list(root.glob('pc_update.py.upload.*')), [])


if __name__ == '__main__':
    unittest.main()
