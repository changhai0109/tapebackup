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

### Experimental Windows backend

The Linux coordinator uses the local index and run plans; a Windows Python
worker accesses the Fibre Channel tape drive. Keep `windows_tape_probe.py`,
`windows_tape_writer.py`, and `windows_backup.py` alongside `tapebackup` when
installing the CLI.

#### Read-only probe

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
general recovery reader. The append worker below is separate from this probe.

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
Windows also supports the explicit-boundary `resume` workflow below; index
queries remain available.

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

#### Continue an existing backup run

`windows_backup.py` implements the Linux-side coordinator and
`windows_tape_writer.py` implements the Windows worker. Invoke them through
the main CLI with `TAPEBACKUP_BACKEND=windows`:

```bash
export TAPEBACKUP_BACKEND=windows
export TAPEBACKUP_RHOST=192.168.1.44 TAPEBACKUP_RUSER=chang TAPEBACKUP_RJUMP=gw

# Supply the currently verified boundary, not an old example's tape address.
./tapebackup -n resume "$RUN_ID" --expected-id "$CARTRIDGE_ID" \
  --start-file "$NEXT_FILE" --start-object "$EOD_LOGICAL_OBJECT" \
  --previous-index "$LAST_INDEX_FILE" --one-chunk

# Remove -n to execute. Omit --one-chunk to finish the remaining run.
```

`--start-file` is the next **1-based tape file number**; `--start-object` is
the logical object address at confirmed EOD in partition 0. `--previous-index`
is the preceding TAPEIDX file, which need not immediately precede the data
file if a separately recorded test archive occupies the gap. The worker checks
both the cartridge identity and the exact EOD position under an exclusive
device handle before any write. It never reclaims a partial tail automatically.

For each chunk, the coordinator:

1. Checks source size/mtime against the saved plan and builds a 1 MiB-block tar.
2. Computes per-file hashes from that exact archive and saves a pending session.
3. Builds the cumulative TAPEIDX archive and uploads both archives to Windows.
4. Verifies staged archive hashes, appends data using SCSI WRITE(6), and flushes
   a synchronous filemark. It then reads the **entire archive** back and compares
   SHA-256 before writing and verifying the index archive the same way.
5. Commits the local session only after both verification records and final EOD
   agree. Successful temporary archives are deleted; journals and manifests
   remain. `--keep-staging` retains the tar files too.

Linux logs/staging live under `TAPEBACKUP_DIR` (default
`~/.cache/tapebackup`). Windows staging defaults to
`%USERPROFILE%/.cache/tapebackup`; `--remote-root C:/path/to/cache` overrides it.
The remote staging path currently must contain no spaces or shell characters.
The worker checks staging disk space before upload. SSH/SFTP and `python` must
be available on Windows. Use an independent systemd user service for long runs.

This workflow requires one worker, a saved run plan and contiguous committed
chunks. It stops on changed sources, pending sessions, transport/device errors,
hash mismatch, or an unexpected position. There is no automatic write retry,
tape changing, blank-tape initialization, `write` command, or destructive tail
repair. A worker's `started` marker prevents accidental replay of an uncertain
append. Inspect the retained pending session and both journals after failure;
do not delete the marker and rerun blindly. If tape verification succeeded but
the local commit was interrupted, reconcile that recorded session first.

The underlying workflow was hardware-tested with a 1 MiB write/readback test
and a 6364-file, approximately 50 GiB chunk plus its cumulative index; both
archives passed full readback SHA-256 verification. The packaged implementation
also has simulated transport tests for wrong media/position, flush failure,
read corruption, invalid manifests, and commit gating. The active long-running
backup uses its separately staged scripts and is not hot-swapped by repo updates.

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).
