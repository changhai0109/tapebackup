# tapebackup

A single-file Python CLI to back up large file sets to **LTO tapes** through a
remote drive host over SSH, with a local SQLite index that makes files
searchable and restorable by path or by run/chunk.

```
tapebackup write /data /home/user/project     # chunked backup (50 GiB chunks)
tapebackup ls [/path]                         # browse the index (fast tree)
tapebackup find PATTERN                       # search paths
tapebackup extract -C DIR PATH | --run RUN    # restore
tapebackup check [TAPE]                       # read-verify recorded archives
tapebackup runs | sessions | tapes            # inventory
tapebackup resume RUN_ID                      # continue an interrupted run
tapebackup erase [-y] [--long]                # erase the loaded tape
tapebackup remove [--yes] [--mtime|--verify-tape] PATH...   # see warning
```

Global flags: `-n/--dry-run`, `-j/--jobs N` (before the command).

## Paths and patterns

Paths starting with `./` or `../` are relative to the current working
directory. For index commands, paths without a leading `/` or `./` refer to
root-relative archive names, even when the original files no longer exist.
Indexed paths remain stored without a leading `/`.

`write` and `remove` accept shell-expanded paths and quoted local patterns,
for example `tapebackup write './project*'`. Unmatched local patterns fail
without operating on any paths. `extract`, `ls`, and `versions` also accept
quoted patterns against the index, e.g.
`tapebackup extract -C /restore './project/*.txt'`. In those commands `*` and `?` match within a single path
component, and hidden names require a leading `.` in that component. Extract
patterns match indexed files, not directory trees. `find` retains substring
search and also accepts shell-style wildcard patterns. Quote patterns when
searching the index, so the shell does not expand them against local files.
`ls` with a pattern, or with multiple paths, lists the matched entries
themselves rather than the contents of matched directories. To browse a
directory, pass it as a single path without a pattern.

Use `--` before literal `write`, `remove`, or `extract` paths that contain
wildcard characters and should not be expanded. Always review `remove` without
`--yes` before allowing deletion.

## ⚠️ WARNING — YOUR DATA IS PRECIOUS

This tool writes to, erases and verifies **tape media**, and `remove`
**deletes local files**. A wrong path, a wrong tape, a stale index, a firmware
quirk or a plain bug can destroy data. There is no undo.

- **Run `remove` without `--yes` first** (dry run) and read the skipped list.
- The default `remove` tier requires a **sha256 recorded at backup time**;
  records without one are refused. Use `--mtime` only when you accept that
  size+mtime is not a content proof, and prefer `--verify-tape`.
- Keep at least one independent copy of anything you cannot afford to lose.
- Double-check which tape is loaded before `erase` or `--force`.
- Run `tapebackup check` on a tape before relying on it.

**THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, INCLUDING DATA
LOSS OR CORRUPTION, ARISING FROM THE USE OF THIS SOFTWARE.**

## Runtime data

The index, directory cache, run plans and the chunk buffer live under:

```
${TAPEBACKUP_DIR:-~/.cache/tapebackup}/
  index.db        authoritative SQLite index (WAL)
  dir_index.db    auxiliary directory tree for fast ls
  runs/<id>/      chunk plans
  tmp/            local tar staging for writes
```

Set `TAPEBACKUP_DIR` to move it. Nothing is stored in this repository.

## Drive host

The tape drive is driven over SSH (`TAPEBACKUP_RHOST`, `TAPEBACKUP_RPORT`,
`TAPEBACKUP_RDEV`, default `/dev/nst0`); the remote host needs `python3`,
`mt`, `sg3_utils` and passwordless sudo for the tape commands. SHA-256
checksums are computed from the exact tar stream that is written, and the
index block is committed to the tape after the data chunk.

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).
