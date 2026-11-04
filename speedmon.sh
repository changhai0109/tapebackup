#!/bin/bash
read_wchar() {
  ssh -p 32824 gw 'for p in $(pgrep -x dd); do sudo -n awk "/^wchar/{print \$2}" /proc/$p/io; done' 2>/dev/null | sort -n | tail -1
}
for i in $(seq 1 7); do
  ts=$(date '+%H:%M:%S')
  a=$(read_wchar); a=${a:-0}
  sleep 20
  b=$(read_wchar); b=${b:-0}
  rate=$(python3 -c "print('%.1f' % (($b-$a)/20/1e6))" 2>/dev/null)
  sent=$(python3 -c "print('%.2f' % ($b/1024**3))" 2>/dev/null)
  chunks=$(sqlite3 ~/tapebackup/index.db "SELECT count(*) FROM sessions WHERE run_id LIKE '08b4693a%';" 2>/dev/null)
  temp=$(tail -1 ~/tapebackup/temp.log 2>/dev/null | awk '{print $3}')
  printf '%s  rate=%6s MB/s  chunk_sent=%7s GiB  chunks=%s/17  tempflag=%s\n' "$ts" "$rate" "$sent" "$chunks" "$temp"
  [ "$i" -lt 7 ] && sleep 280
done
