#!/bin/bash
read_wchar() {
  ssh -p 32824 gw 'for p in $(pgrep -x dd); do sudo -n awk "/^wchar/{print \$2}" /proc/$p/io; done' 2>/dev/null | sort -n | tail -1
}
prev=0; ticks=0
while true; do
  n=$(sqlite3 "$HOME/tapebackup/index.db" "SELECT count(*) FROM sessions WHERE run_id LIKE '08b4693a%';" 2>/dev/null)
  if [ "${n:-0}" -ge 11 ]; then
    echo "CHUNK11_DONE chunks=$n at $(date '+%H:%M:%S')"
    # pause the retry loop and the writer before chunk 12 starts
    LP=$(ps -eo pid,args | awk '$2=="bash" && $0 ~ /run-forever.sh/ {print $1; exit}')
    [ -n "$LP" ] && kill -TERM $LP 2>/dev/null && echo "stopped loop pid=$LP"
    RP=$(ps -eo pid,comm,args | awk '$2=="python3" && $4=="resume" {print $1; exit}')
    [ -n "$RP" ] && kill -TERM -$RP 2>/dev/null && echo "stopped resume pid=$RP"
    sleep 3
    for p in $(ps -eo pid,args | grep -E "[t]ar -c -b2048|[s]sh .*sudo dd of=/dev/nst0|[s]sh .*flock -x" | awk '{print $1}'); do
      kill -TERM $p 2>/dev/null
    done
    sleep 2
    echo "PAUSED — safe to power-cycle the drive"
    break
  fi
  b=$(read_wchar); b=${b:-0}
  if [ $((ticks % 6)) -eq 0 ]; then
    if [ "$prev" != "0" ]; then
      rate=$(python3 -c "print('%.1f' % (($b-$prev)/120/1e6))" 2>/dev/null)
    else rate="?"; fi
    printf '%s  chunks=%s/17  chunk_sent=%.2f GiB  rate=%s MB/s\n' "$(date '+%H:%M:%S')" "$n" "$(python3 -c "print($b/1024**3)" 2>/dev/null)" "$rate"
  fi
  prev=$b; ticks=$((ticks+1))
  sleep 20
done
