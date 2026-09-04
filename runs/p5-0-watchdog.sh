#!/usr/bin/env bash
# P5-0 回归看门狗 v2:跑指定 tier,环境自愈后续跑。
# v1 教训(2026-08-24 实证):health 窗口注册数 ≠ 命令能跑通——App 被重启后停在
# home 标签、云端工程未恢复,窗口照常注册,但 sch pages --project 全 NO_CONNECTOR;
# v1 探针(wins>0)误判"活着",10 次空转一次恢复都没触发。
# v2 探针=真实往返:probe(sch pages --project)==_health_check 同款 + 前台须为原理图。
# 恢复分级:轻=doc open 重绑工程前台;重=优雅关 App→explorer 重启→等探针绿
# (优雅关给保存会话机会;taskkill //F 丢会话,云端工程重启后不会自动恢复,
#  且 1.1.1 CLI/动作目录均无 project.open——重恢复失败时 App 内弹 toast 求人工)。
# resume 语义:只跳 PASS,HALT/未跑的重试。用法: bash runs/p5-0-watchdog.sh <tier> <log>
set -u
TIER="${1:?tier}"
LOG="${2:?log}"
PY=./.venv/Scripts/python.exe
EASYEDA=/c/Users/admin/.local/bin/easyeda.exe
PROJ=edaloop
PAGE=P1

count() {  # 输出 "PASS数 总数"
  $PY -c "
import json
s=json.load(open('runs/w3-loop-state-$TIER.json',encoding='utf-8'))
rows=s.get('rows') or {}
vals=list(rows.values()) if isinstance(rows,dict) else rows
print(sum(1 for r in vals if r.get('status')=='PASS'),len(vals))
" 2>/dev/null || echo "0 0"
}

probe() {  # 真实探针:与 eval _health_check 同款(sch pages 往返 + --project 路由)
  $EASYEDA sch pages --project $PROJ >/dev/null 2>&1
}

foreground_ok() {  # 前台必须是原理图页,否则 warmup 的 sch clear 会被拒
  $EASYEDA project doc 2>/dev/null | $PY -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: raise SystemExit(1)
r=d.get('result') or {}
raise SystemExit(0 if r.get('documentType')=='schematic' else 1)
"
}

winid() {  # 取第一个已注册窗口 id(notify 用;工程未开时 --project 路由不可用)
  $EASYEDA health 2>/dev/null | $PY -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: raise SystemExit(0)
w=(((d.get('found') or {}).get('raw') or {}).get('windows')) or []
print(w[0]['windowId'] if w else '')
"
}

bind_project() {  # 工程已开时把前台切到原理图页
  $EASYEDA doc open $PAGE --project $PROJ >> "$LOG" 2>&1
  sleep 4
}

recover() {
  echo "[watchdog $(date +%H:%M:%S)] 环境不自洽,尝试恢复" >> "$LOG"
  # 轻恢复:工程可能开着,只是前台丢了
  bind_project
  probe && foreground_ok && { echo "[watchdog] 轻恢复成功(重绑前台)" >> "$LOG"; return 0; }
  # 重恢复:App 假死或工程未开 → 优雅关→重启
  echo "[watchdog] 轻恢复无效,重启 EasyEDA(先优雅关再强杀)" >> "$LOG"
  taskkill //IM lceda-pro.exe >> "$LOG" 2>&1
  for i in $(seq 1 6); do
    sleep 5
    tasklist //FI "IMAGENAME eq lceda-pro.exe" 2>/dev/null | grep -q lceda || break
    [ $i -eq 6 ] && taskkill //F //IM lceda-pro.exe >> "$LOG" 2>&1
  done
  sleep 3
  explorer.exe "D:\\lceda-pro\\lceda-pro.exe"
  for i in $(seq 1 18); do  # 180 秒恢复窗口
    sleep 10
    bind_project
    if probe && foreground_ok; then
      echo "[watchdog] 重启恢复成功(${i}0秒内)" >> "$LOG"; return 0
    fi
    if [ $((i % 6)) -eq 0 ]; then  # 每 60 秒在 App 里弹 toast 求人工
      w=$(winid)
      [ -n "$w" ] && $EASYEDA notify --window "$w" --type warn --duration 10 \
        --message "回归看门狗:请打开工程 $PROJ 并切到原理图页(云端工程重启后不自动恢复)" >> "$LOG" 2>&1
    fi
  done
  echo "[watchdog] 恢复失败(大概率:云端工程未恢复且 CLI 无法开工程)——转人工:请在 EasyEDA 打开 $PROJ 后重跑 bash runs/p5-0-watchdog.sh $TIER $LOG" >> "$LOG"
  return 1
}

for attempt in $(seq 1 10); do
  echo "[watchdog $(date +%H:%M:%S)] attempt $attempt 开跑" >> "$LOG"
  probe && foreground_ok || { recover || { echo "[watchdog] 转人工" >> "$LOG"; exit 1; }; }
  $PY -m edaloop.cli eval --subset w3-loop --tier "$TIER" >> "$LOG" 2>&1
  rc=$?
  read p t <<< "$(count)"
  echo "[watchdog $(date +%H:%M:%S)] attempt $attempt exit=$rc 进度 $p/$t" >> "$LOG"
  if [ "$t" -gt 0 ] && [ "$p" = "$t" ]; then
    echo "[watchdog] 全部 PASS,收工" >> "$LOG"
    exit 0
  fi
  probe || recover || { echo "[watchdog] 转人工" >> "$LOG"; exit 1; }
  sleep 30  # 探针活着但没全绿:可能是 lib-search 限流抖动,歇 30 秒再试
done
echo "[watchdog] 10 次尝试后仍未全绿,转人工" >> "$LOG"
exit 1
