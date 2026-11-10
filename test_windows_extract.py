import hashlib
import os
import sqlite3
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from test_paths import tb


class WindowsExtractTests(unittest.TestCase):
    def restore(self, dest, data, returncode=0, path="a/file"):
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE sessions(sid TEXT, status INTEGER)")
        c.execute("INSERT INTO sessions VALUES('sid',1)")
        c.execute("CREATE TABLE file_index(sid TEXT,path TEXT,size INTEGER,sum TEXT)")
        c.execute("INSERT INTO file_index VALUES(?,?,?,?)",
                  ("sid", path, 4, hashlib.sha256(b"good").hexdigest()))
        ctx = types.SimpleNamespace(rhost="windows", dry=False,
                                    ssh_argv_stream=lambda: ["ssh", "windows"])

        def transport(argv, input, stdout):
            stdout.write(data)
            return subprocess.CompletedProcess(argv, returncode)

        with mock.patch.object(tb, "db_open", return_value=c), \
                mock.patch.object(tb.subprocess, "run", side_effect=transport) as run:
            tb.windows_extract(ctx, dest, "sid", [(1, "SONY:test", 2)], [path])
            return run.call_count

    def test_verified_file_published(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.restore(d, b"good"), 1)
            with open(os.path.join(d, "a/file"), "rb") as f:
                self.assertEqual(f.read(), b"good")

    def test_corruption_and_transport_failure_leave_no_file(self):
        for data, rc in ((b"evil", 0), (b"good", 1)):
            with self.subTest(data=data, rc=rc), tempfile.TemporaryDirectory() as d:
                with self.assertRaises(SystemExit):
                    self.restore(d, data, rc)
                self.assertEqual(os.listdir(os.path.join(d, "a")), [])

    def test_existing_file_and_escaping_symlink_refused(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outside:
            os.symlink(outside, os.path.join(d, "a"))
            with self.assertRaises(SystemExit):
                self.restore(d, b"good")
            self.assertEqual(os.listdir(outside), [])
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "existing"), "wb") as f:
                f.write(b"keep")
            with self.assertRaises(SystemExit):
                self.restore(d, b"good", path="existing")
            with open(os.path.join(d, "existing"), "rb") as f:
                self.assertEqual(f.read(), b"keep")


if __name__ == "__main__":
    unittest.main()
