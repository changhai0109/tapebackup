#!/usr/bin/env python3
"""Linux coordinator for experimental Windows continuation of an existing run."""
import argparse
import fcntl
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

from windows_tape_writer import BLOCK, digest_file


def load_api():
    loader = importlib.machinery.SourceFileLoader("tapebackup_windows", os.path.join(os.path.dirname(__file__), "tapebackup"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    api = importlib.util.module_from_spec(spec)
    loader.exec_module(api)
    return api


def remote_path(path):
    path = path.replace("\\", "/")
    if not re.fullmatch(r"[A-Za-z]:/[A-Za-z0-9_./-]+", path) or ".." in path.split("/"):
        raise ValueError("remote staging path must be an absolute Windows path without spaces or shell characters")
    return path.rstrip("/")


def verified_result(journal, config):
    if not journal or journal[-1].get("stage") != "success" or journal[-1].get("sid") != config["sid"]:
        raise RuntimeError("remote success record missing")
    verified = {r["archive"]: r["sha256"] for r in journal if r.get("stage") == "archive_verified"}
    if verified != {a["name"]: a["sha256"] for a in config["archives"]}:
        raise RuntimeError("remote verification records mismatch")
    expected = [0, config["start_logical_object"] + sum(a["size"] // BLOCK + 1 for a in config["archives"]),
                config["data_file"] + 1]
    if journal[-1].get("eod") != expected:
        raise RuntimeError("unexpected final EOD")
    return expected


def execute(argv, **kwargs):
    return subprocess.run(argv, check=True, **kwargs)


def resume(api, ctx, args):
    if not ctx.rhost or ctx.jobs != 1:
        raise ValueError("Windows resume requires TAPEBACKUP_RHOST and one worker")
    if not re.fullmatch(r"\\\\\.\\Tape[0-9]+", args.device):
        raise ValueError("invalid Windows tape device")
    if args.start_file < 2 or args.start_object < 1 or not 0 < args.previous_index < args.start_file:
        raise ValueError("invalid expected append boundary")
    c = api.db_open(ctx)
    try:
        runs = c.execute("SELECT run_id,n_chunks FROM runs WHERE run_id LIKE ?", (args.run + "%",)).fetchall()
        if len(runs) != 1:
            raise ValueError("run must match exactly one existing run")
        run_id, total = runs[0]
        rows = c.execute("SELECT chunk_seq,status FROM sessions WHERE run_id=? ORDER BY chunk_seq", (run_id,)).fetchall()
        if rows != [(n, 1) for n in range(1, len(rows) + 1)]:
            raise RuntimeError("run has pending, duplicate or non-contiguous sessions; inspect before continuing")
        completed = len(rows)
    finally:
        c.close()
    if completed >= total:
        print("run already complete")
        return
    if ctx.dry:
        print("[dry-run] Windows resume %s chunks %d..%d at tape %s file %d, logical object %d" %
              (run_id[:8], completed + 1, completed + 1 if args.one_chunk else total,
               args.expected_id, args.start_file, args.start_object))
        return
    os.makedirs(ctx.dir, exist_ok=True)
    with open(os.path.join(ctx.dir, "drive.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        root = tempfile.mkdtemp(prefix="windows-resume-%s-" % run_id[:8], dir=ctx.dir)
        print("Windows resume logs: %s" % root, flush=True)

        def log(stage, **data):
            data.update(stage=stage, time=time.strftime("%Y-%m-%d %H:%M:%S"))
            with open(os.path.join(root, "progress.jsonl"), "a") as f:
                f.write(json.dumps(data) + "\n"); f.flush(); os.fsync(f.fileno())
            print(json.dumps(data), flush=True)

        ssh = ctx.ssh_argv_stream()
        if args.remote_root:
            remote_root = remote_path(args.remote_root)
        else:
            result = execute(ssh + ["python -"], input=b'import os,json\nprint(json.dumps(os.path.join(os.environ["USERPROFILE"],".cache","tapebackup")))\n', stdout=subprocess.PIPE)
            remote_root = remote_path(json.loads(result.stdout))
        scp = ["scp", "-o", "BatchMode=yes"]
        if ctx.rport:
            scp += ["-P", str(ctx.rport)]
        if os.environ.get("TAPEBACKUP_RJUMP"):
            scp += ["-J", os.environ["TAPEBACKUP_RJUMP"]]
        host = (ctx.ruser + "@" if ctx.ruser else "") + ctx.rhost
        repo = os.path.dirname(os.path.realpath(__file__))
        start_object, data_file, previous_index = args.start_object, args.start_file, args.previous_index
        try:
            for seq in range(completed + 1, (completed + 1 if args.one_chunk else total) + 1):
                work = os.path.join(root, "chunk-%d" % seq)
                os.mkdir(work)
                sid = str(uuid.uuid4())
                log("preparing", chunk=seq, sid=sid, data_file=data_file)
                files = []
                with open(os.path.join(ctx.dir, "runs", run_id, "plan.tsv")) as plan:
                    for line in plan:
                        number, path, size, mtime = line.rstrip("\n").split("\t")
                        if int(number) != seq:
                            continue
                        path, size, mtime = api.unesc(path), int(size), int(mtime)
                        info = os.lstat(path)
                        if not stat.S_ISREG(info.st_mode) or (info.st_size, int(info.st_mtime)) != (size, mtime):
                            raise RuntimeError("source changed: " + path)
                        files.append((path, size, mtime))
                if not files:
                    raise RuntimeError("empty chunk plan")
                with open(os.path.join(work, "chunk.list"), "wb") as out:
                    for path, size, mtime in files:
                        out.write(os.fsencode(path) + b"\0")
                archive = os.path.join(work, "chunk.tar")
                execute(["tar", "-c", "--hard-dereference", "-b2048", "--null", "-T", os.path.join(work, "chunk.list"), "-f", archive])
                meta = api._parse_chunk_tar(archive)
                if len(meta) != len(files) or any(meta.get(p.lstrip("/"), ())[:2] != (sz, mt) for p, sz, mt in files):
                    raise RuntimeError("archive/source metadata mismatch")
                datahash = digest_file(archive)
                c = api.db_open(ctx)
                try:
                    c.execute("BEGIN IMMEDIATE")
                    if c.execute("SELECT 1 FROM sessions WHERE run_id=? AND chunk_seq=?", (run_id, seq)).fetchone():
                        raise RuntimeError("chunk already has a session")
                    c.execute("INSERT INTO sessions(sid,host_uuid,host_name,started,finished,paths,est_size,est_files,status,run_id,chunk_seq) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                              (sid, ctx.host_uuid, ctx.host_name, time.strftime("%Y-%m-%d %H:%M:%S"), None,
                               "chunk %d/%d of run %s" % (seq, total, run_id[:8]), sum(f[1] for f in files), len(files), 0, run_id, seq))
                    c.execute("INSERT INTO session_tapes VALUES(?,?,?,?)", (sid, 1, args.expected_id, data_file))
                    c.executemany("INSERT INTO file_index(sid,path,size,mtime,sum) VALUES(?,?,?,?,?)", [(sid, p.lstrip("/"), *meta[p.lstrip("/")]) for p, sz, mt in files])
                    c.commit()
                finally:
                    c.close()
                log("pending_session_saved", chunk=seq, sid=sid)
                bdir = api.block_dump(ctx, work, args.expected_id, "", data_file + 1, previous_index, force_status_sid=sid)
                if not bdir:
                    raise RuntimeError("index generation failed")
                execute(["tar", "-c", "-b2048", "-C", work, "-f", os.path.join(work, "index.tar"), os.path.basename(bdir)])
                config = {"sid": sid, "run_id": run_id, "chunk": seq, "cartridge": args.expected_id,
                          "start_logical_object": start_object, "data_file": data_file,
                          "index_file": data_file + 1, "previous_index_file": previous_index, "archives": []}
                for name in ("chunk.tar", "index.tar"):
                    path = os.path.join(work, name)
                    config["archives"].append({"name": name, "size": os.path.getsize(path), "sha256": datahash if name == "chunk.tar" else digest_file(path)})
                with open(os.path.join(work, "manifest.json"), "x") as f:
                    json.dump(config, f, indent=2)
                remote = remote_root + "/" + sid
                needed = sum(a["size"] for a in config["archives"]) + 2 * BLOCK * 1024
                script = "import os,shutil\nos.makedirs(%r,exist_ok=True)\nassert shutil.disk_usage(%r).free>%d, 'insufficient staging space'\nos.mkdir(%r)\n" % (remote_root, remote_root, needed, remote)
                execute(ssh + ["python -"], input=script.encode())
                execute(scp + [os.path.join(work, n) for n in ("chunk.tar", "index.tar", "manifest.json")] +
                        [os.path.join(repo, n) for n in ("windows_tape_writer.py", "windows_tape_probe.py")] + [host + ":" + remote + "/"])
                log("upload_complete", chunk=seq, remote_directory=remote)
                with open(os.path.join(work, "remote-journal.jsonl"), "xb") as out:
                    execute(ssh + ["python %s/windows_tape_writer.py %s --device %s" % (remote, remote, args.device)], stdout=out)
                with open(os.path.join(work, "remote-journal.jsonl")) as f:
                    eod = verified_result([json.loads(line) for line in f], config)
                c = api.db_open(ctx)
                try:
                    c.execute("BEGIN IMMEDIATE")
                    changed = c.execute("UPDATE sessions SET status=1,finished=? WHERE sid=? AND status=0", (time.strftime("%Y-%m-%d %H:%M:%S"), sid))
                    if changed.rowcount != 1:
                        raise RuntimeError("pending session changed")
                    c.commit()
                finally:
                    c.close()
                log("committed", chunk=seq, sid=sid, eod=eod)
                start_object, previous_index, data_file = eod[1], eod[2], eod[2] + 1
                if not args.keep_staging:
                    for name in ("chunk.tar", "index.tar"):
                        os.unlink(os.path.join(work, name))
                    shutil.rmtree(bdir)
                    execute(ssh + ["python -"], input=("import os\nfor name in ('chunk.tar','index.tar'):os.unlink(os.path.join(%r,name))\n" % remote).encode())
            if seq == total:
                c = api.db_open(ctx)
                try:
                    c.execute("UPDATE runs SET status=1,finished=? WHERE run_id=?", (time.strftime("%Y-%m-%d %H:%M:%S"), run_id))
                    c.commit()
                finally:
                    c.close()
            log("finished", completed_through=seq, total=total)
        except BaseException as error:
            log("stopped_on_error", error=str(error), action="inspect pending sessions and journals; no automatic write retry")
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run")
    parser.add_argument("--expected-id", required=True)
    parser.add_argument("--start-file", type=int, required=True)
    parser.add_argument("--start-object", type=int, required=True)
    parser.add_argument("--previous-index", type=int, required=True)
    parser.add_argument("--one-chunk", action="store_true")
    parser.add_argument("--keep-staging", action="store_true")
    parser.add_argument("--remote-root")
    parser.add_argument("--device", default=os.environ.get("TAPEBACKUP_WINDEV", r"\\.\Tape0"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    api = load_api()
    resume(api, api.Ctx(dry=args.dry_run), args)


if __name__ == "__main__":
    main()
