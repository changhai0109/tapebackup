#!/bin/bash
export TAPEBACKUP_RHOST=gw
export TAPEBACKUP_RPORT=32824
export PYTHONUNBUFFERED=1
LOG="$HOME/tapebackup/write-backup-resume-$(date +%F-%H%M).log"
echo "log: $LOG" >"$LOG"
exec /usr/local/bin/tapebackup resume 08b4693a --cooldown 90 >>"$LOG" 2>&1
