#!/usr/bin/env bash
# 等待用户在 EasyEDA 里打开 edeloop 工程(rc=0 判据),然后自动续跑 rest 回归
set -u
for i in $(seq 1 90); do  # 最多 90 分钟
  if /c/Users/admin/.local/bin/easyeda.exe sch pages --project edeloop >/dev/null 2>&1; then
    echo "[sentinel $(date +%H:%M:%S)] 工程已恢复(${i}分钟内),续跑 rest"
    exec bash runs/p5-0-watchdog.sh rest runs/log-rest-b3.log
  fi
  sleep 60
done
echo "[sentinel] 90 分钟无恢复,退出"
exit 1
