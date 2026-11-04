#!/bin/bash
export TAPEBACKUP_RHOST=gw
export TAPEBACKUP_RPORT=32824
export PYTHONUNBUFFERED=1
LOG="$HOME/tapebackup/write-backup-loop-$(date +%F-%H%M).log"
echo "loop log: $LOG" >"$LOG"
while true; do
  echo "=== resume attempt $(date) ===" >>"$LOG"
  /usr/local/bin/tapebackup resume 08b4693a --cooldown 90 >>"$LOG" 2>&1
  st=$(sqlite3 "$HOME/tapebackup/index.db" "SELECT status FROM runs WHERE run_id LIKE '08b4693a%';" 2>/dev/null)
  if [ "$st" = "1" ]; then
    echo "=== run complete $(date) ===" >>"$LOG"
    break
  fi
  echo "=== attempt ended (status=$st), retry in 120s ===" >>"$LOG"
  sleep 120
done
