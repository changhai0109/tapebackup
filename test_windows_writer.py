import contextlib
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import types
import unittest
from unittest import mock

import windows_backup as backup
import windows_tape_writer as writer


class FakeTape:
    def __init__(self, identity="TEST:1", start=100, corrupt=False, fail_mark=False):
        self.cid = identity
        self.cursor = start
        self.end = start
        self.objects = {}
        self.writes = 0
        self.corrupt = corrupt
        self.fail_mark = fail_mark

    def identity(self):
        return self.cid

    def ready(self):
        pass

    def seek(self, method, count=0):
        self.cursor = self.end if method == 4 else count

    def position(self):
        return [0, self.cursor, 1 + sum(v is None for k, v in self.objects.items() if k < self.cursor)]

    def write(self, data):
        assert self.cursor == self.end
        self.objects[self.cursor] = data
        self.cursor += 1
        self.end = self.cursor
        self.writes += 1

    def write_filemark(self):
        if self.fail_mark:
            raise RuntimeError("filemark flush failed")
        self.objects[self.cursor] = None
        self.cursor += 1
        self.end = self.cursor

    def read(self):
        data = self.objects[self.cursor]
        self.cursor += 1
        return b"!" + data[1:] if self.corrupt else data

    def expect_filemark(self):
        assert self.objects[self.cursor] is None
        self.cursor += 1


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {"sid": "session", "cartridge": "TEST:1", "data_file": 2,
                       "start_logical_object": 100, "archives": []}
        for name, byte in (("chunk.tar", b"a"), ("index.tar", b"b")):
            data = byte * writer.BLOCK
            with open(os.path.join(self.temp.name, name), "wb") as f:
                f.write(data)
            self.config["archives"].append({"name": name, "size": len(data),
                                             "sha256": hashlib.sha256(data).hexdigest()})
        self.events = []

    def append(self, tape):
        return writer.append_verified(self.temp.name, self.config, tape,
                                      lambda stage, **kw: self.events.append(dict(stage=stage, **kw)))

    def test_both_archives_verified_before_success(self):
        tape = FakeTape()
        self.assertEqual(self.append(tape), [0, 104, 3])
        self.assertEqual(tape.writes, 2)
        self.assertEqual(backup.verified_result(self.events, self.config), [0, 104, 3])

    def test_identity_and_eod_mismatch_do_not_write(self):
        for tape in (FakeTape(identity="OTHER"), FakeTape(start=101)):
            with self.assertRaises(RuntimeError):
                self.append(tape)
            self.assertEqual(tape.writes, 0)

    def test_readback_corruption_prevents_index_write_and_commit(self):
        tape = FakeTape(corrupt=True)
        with self.assertRaisesRegex(RuntimeError, "SHA-256"):
            self.append(tape)
        self.assertEqual(tape.writes, 1)
        with self.assertRaises(RuntimeError):
            backup.verified_result(self.events, self.config)

    def test_flush_failure_is_not_retried(self):
        tape = FakeTape(fail_mark=True)
        with self.assertRaisesRegex(RuntimeError, "flush"):
            self.append(tape)
        self.assertEqual(tape.writes, 1)
        self.assertFalse(any(r["stage"] == "success" for r in self.events))

    def test_wrong_success_eod_cannot_commit(self):
        self.append(FakeTape())
        self.events[-1]["eod"] = [0, 999, 3]
        with self.assertRaisesRegex(RuntimeError, "EOD"):
            backup.verified_result(self.events, self.config)

    def test_invalid_archive_name_and_size_refused(self):
        self.config["archives"][0]["name"] = "../chunk.tar"
        with self.assertRaises(ValueError):
            self.append(FakeTape())
        self.config["archives"][0]["name"] = "chunk.tar"
        self.config["archives"][0]["size"] = writer.BLOCK - 1
        with self.assertRaises(ValueError):
            self.append(FakeTape())

    def test_unknown_position_flags_and_short_position_refused(self):
        for data in (b"\0" * 31, b"\x04" + b"\0" * 31, b"\x08" + b"\0" * 31):
            with self.assertRaises(RuntimeError):
                writer.parse_position(data)

    def test_remote_shell_characters_refused(self):
        self.assertEqual(backup.remote_path(r"C:\Users\test\.cache\tapebackup"),
                         "C:/Users/test/.cache/tapebackup")
        for path in ("C:/temp/../other", "C:/temp&whoami", "C:/two words", "relative"):
            with self.assertRaises(ValueError):
                backup.remote_path(path)

    def test_resume_dry_run_never_calls_transport(self):
        db = os.path.join(self.temp.name, "db")
        c = sqlite3.connect(db)
        c.executescript("CREATE TABLE runs(run_id TEXT,n_chunks INTEGER);"
                        "CREATE TABLE sessions(run_id TEXT,chunk_seq INTEGER,status INTEGER);"
                        "INSERT INTO runs VALUES('test-run',2);"
                        "INSERT INTO sessions VALUES('test-run',1,1);")
        c.close()
        api = types.SimpleNamespace(db_open=lambda ctx: sqlite3.connect(db))
        ctx = types.SimpleNamespace(rhost="windows", jobs=1, dry=True)
        args = types.SimpleNamespace(run="test", device=r"\\.\Tape0", start_file=4,
                                     start_object=100, previous_index=3,
                                     expected_id="TEST:1", one_chunk=False)
        with mock.patch.object(backup, "execute") as transport, contextlib.redirect_stdout(io.StringIO()):
            backup.resume(api, ctx, args)
        transport.assert_not_called()
        c = sqlite3.connect(db)
        c.execute("INSERT INTO sessions VALUES('test-run',2,0)")
        c.commit(); c.close()
        with self.assertRaisesRegex(RuntimeError, "pending"):
            backup.resume(api, ctx, args)


if __name__ == "__main__":
    unittest.main()
