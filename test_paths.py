import contextlib
import importlib.machinery
import importlib.util
import io
import os
import sqlite3
import tempfile
import types
import unittest
from unittest import mock


loader = importlib.machinery.SourceFileLoader("tapebackup_test", os.path.join(os.path.dirname(__file__), "tapebackup"))
spec = importlib.util.spec_from_loader(loader.name, loader)
tb = importlib.util.module_from_spec(spec)
loader.exec_module(tb)


class PathTests(unittest.TestCase):
    def test_relative_index_path_does_not_depend_on_existing_file(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("os.getcwd", return_value=d):
            self.assertEqual(tb._index_path("./gone/file"), (d + "/gone/file").lstrip("/"))
            self.assertEqual(tb._index_path("home/user/gone"), "home/user/gone")

    def test_index_glob_respects_components_and_hidden_files(self):
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE file_index(path TEXT)")
        c.executemany("INSERT INTO file_index VALUES(?)", [(x,) for x in
                      ("home/u/a.txt", "home/u/.hidden.txt", "home/u/dir/b.txt")])
        self.assertEqual(tb._index_matches(c, "home/u/*.txt"), ["home/u/a.txt"])
        self.assertEqual(tb._index_matches(c, "home/u/.*"), ["home/u/.hidden.txt"])
        self.assertEqual(tb._index_matches(c, "home/u/dir/*.txt"), ["home/u/dir/b.txt"])
        c.close()

    def test_local_glob_and_unmatched_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("a.txt", "b.txt", ".c.txt"):
                open(os.path.join(d, name), "w").close()
            self.assertEqual(tb._local_patterns([d + "/*.txt"]), [d + "/a.txt", d + "/b.txt"])
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                tb._local_patterns([d + "/missing*"])

    def test_extract_pattern_spanning_sessions_dry_run(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "index.db")
            c = sqlite3.connect(db)
            c.executescript("""
                CREATE TABLE file_index(sid TEXT,path TEXT);
                CREATE TABLE sessions(sid TEXT,started TEXT,status INTEGER);
                CREATE TABLE session_tapes(sid TEXT,seq INTEGER,tape TEXT,file_no INTEGER);
                INSERT INTO sessions VALUES('one','2026-01-01',1),('two','2026-01-02',1);
                INSERT INTO session_tapes VALUES('one',1,'tape',2),('two',1,'tape',4);
                INSERT INTO file_index VALUES('one','gone/a.txt'),('two','gone/b.txt');
            """)
            c.commit()
            c.close()
            output = io.StringIO()
            with mock.patch.object(tb, "db_open", side_effect=lambda ctx: sqlite3.connect(db)), \
                 contextlib.redirect_stdout(output):
                tb.cmd_extract(types.SimpleNamespace(dry=True), ["-C", d, "gone/*.txt"])
            self.assertEqual(output.getvalue().count("[dry-run] would seek"), 2)

    def test_ls_glob_and_multiple_args_list_entries_not_children(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "dirs.db")
            c = sqlite3.connect(db)
            c.executescript("""
                CREATE TABLE dir_index(host TEXT,parent TEXT,name TEXT,is_dir INTEGER,size INTEGER,n_files INTEGER);
                INSERT INTO dir_index VALUES
                    ('host','home/user','proj-a',1,10,1),
                    ('host','home/user','proj-b',1,20,1),
                    ('host','home/user/proj-a','inside.txt',0,10,0);
            """)
            c.commit()
            c.close()
            for args in (["home/user/proj-*"], ["home/user/proj-a", "home/user/proj-b"]):
                output = io.StringIO()
                with mock.patch.object(tb, "db_open", side_effect=lambda ctx: sqlite3.connect(db)), \
                     mock.patch.object(tb, "dir_index_open", side_effect=lambda ctx: sqlite3.connect(db)), \
                     mock.patch.object(tb, "ensure_dir_index"), contextlib.redirect_stdout(output):
                    tb.cmd_ls(None, args)
                self.assertIn("home/user/proj-a/", output.getvalue())
                self.assertIn("home/user/proj-b/", output.getvalue())
                self.assertNotIn("inside.txt", output.getvalue())


if __name__ == "__main__":
    unittest.main()
