#!/bin/bash
export TAPEBACKUP_RHOST=gw
export TAPEBACKUP_RPORT=32824
export PYTHONUNBUFFERED=1
LOG="$HOME/tapebackup/write-backup-$(date +%F-%H%M).log"
echo "log: $LOG" >"$LOG"
exec /usr/local/bin/tapebackup write /data/share/changhai/backup >>"$LOG" 2>&1
