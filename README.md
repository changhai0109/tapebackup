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

### Experimental Windows read-only probe

`windows_tape_probe.py` runs on Windows with Python 3 and uses read-only SCSI
INQUIRY, READ ATTRIBUTE and LOG SENSE commands to report the drive identity,
MAM cartridge identity and LTO capacity log as JSON:

```powershell
python windows_tape_probe.py --device '\\.\Tape0'
```

Opening the tape device may require administrator privileges and the Windows
tape driver must support SCSI pass-through. The program opens the device with
read/write access as required by the Windows pass-through interface, but does
not issue write, erase or media-load commands. The default probe does not move
tape. Do not run it concurrently with other tape software.

The experimental `--sample MEMBER --file-number N --expected-id ID` mode
rewinds and spaces to a 1-based tape file, then streams one regular tar member
to binary stdout using SCSI READ(6). Capture stdout with a binary-safe process
API and compare its size and SHA-256 with the index; discard output on any error.
It requires at least a 1 MiB adapter transfer limit and fails closed on SCSI
check conditions (including truncated records and filemarks). This is not a
general recovery reader. Windows writing and interrupted-session recovery are
not implemented.

#### Restore through the main CLI

Run the main CLI on Linux (where the index lives) with the Windows reader next
to the `tapebackup` script. Python 3 must be available as `python` on Windows:

```bash
mkdir -p /path/to/restore
TAPEBACKUP_BACKEND=windows \
TAPEBACKUP_RHOST=192.168.1.44 TAPEBACKUP_RUSER=chang TAPEBACKUP_RJUMP=gw \
  ./tapebackup extract -C /path/to/restore --sid SESSION_ID -- home/user/file
```

Omit `TAPEBACKUP_RJUMP` when no SSH jump host is needed. `TAPEBACKUP_WINDEV`
defaults to `\\.\Tape0`. The helper is sent over SSH automatically; it does not
need installing on Windows. SSH host keys must already be trusted.

The Windows backend supports explicit regular-file paths and quoted index
globs, with optional `--sid`/`--at`, for committed single-volume sessions with
backup-time SHA-256. It restores file contents into root-relative paths under
`-C`, verifies both byte count and hash, and publishes each file only after
verification. Existing destination files are refused. It currently reads one
file at a time, rewinding for each; directory/session-wide extraction, metadata
restoration, multi-volume archives, and `--run` extraction are not supported.
Use `-n` before `extract` to inspect the plan without accessing the tape.
Other Windows tape operations are blocked; index queries remain available.

On 2026-11-10, five randomly selected indexed files across tape files
54, 66, 144 and 170 were restored through this CLI; all five passed size and
SHA-256 verification, including an independent post-restore check.

On the tested HP OEM QLogic 2532 with `ql2300` 9.2.5.20, `MaximumSGList=255`
did not raise the 512 KiB limit. After backing up the registry, setting the
`DriverParameter` string to `ts=5;` under
`HKLM\SYSTEM\CurrentControlSet\Services\ql2300\Parameters\Device` and rebooting
raised the reported limit to 1048576 bytes / 257 physical pages. This is a
version-specific observation, not a universal driver setting; preserve any
existing driver parameters. `ReadFile` still failed on this setup, while SCSI
READ(6) restored a 150165-byte sample from tape file 116 whose SHA-256 matched
the backup index (2026-11-10). No driver upgrade was needed.

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).
