#!/bin/bash
# Wait for the stage_backups.tgz resume run to finish, then back up the code dirs.
set -u
RPID=${1:?resume pid}
while kill -0 "$RPID" 2>/dev/null; do sleep 30; done
sleep 5
cd /home/changhai/tapebackup || exit 1
SHIFTED=${2:-/tmp/opencode/tb_in2}
export TAPEBACKUP_RHOST=gw TAPEBACKUP_RPORT=32826 PYTHONUNBUFFERED=1
exec ./tapebackup write \
  /home/changhai/code/stage_artifact \
  /home/changhai/code/stage_artifact_copy \
  /home/changhai/code/stage_artifact_wip \
  /home/changhai/code/stage_demo \
  /home/changhai/code/stg \
  /home/changhai/code/stg_submission \
  /home/changhai/code/stgwip \
  /home/changhai/code/symbolic_tensor_graph \
  /home/changhai/code/symbolic_tensor_graph_2 \
  /home/changhai/code/symbolic_tensor_graph_250825.tgz \
  /home/changhai/code/symbolic_tensor_graph_huan \
  /home/changhai/code/symbolic_tensor_graph.tgz \
  < "$SHIFTED"
