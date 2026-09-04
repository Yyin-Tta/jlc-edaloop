#!/usr/bin/env bash
# 双机同步:docs/ 与 runs/ 两个嵌套私有仓一条龙。
# 开工、收工各跑一次均可(幂等:无改动只 pull,有改动先 pull 再 commit 后 push)。
# 前提:docs/ 与 runs/ 各自已是嵌套仓(有 .git)——runs/ 无 .git 时 git -C 会落到
# 主仓,add -A 会把 easyeda-agent//samples//.easyeda/ 吞进主仓,先按 §5.4 处理。
# 用法: bash docs/sync.sh ["提交说明"]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MSG="${1:-sync: $(date '+%F %H:%M')}"

for d in docs runs; do
  echo "== $d =="
  # 先拉:远端收进来,本地未提交改动由 autostash 保护;失败(冲突/撞名)跳过该仓,
  # 绝不带着失败状态往下 push。
  if ! git -C "$ROOT/$d" pull --rebase --autostash; then
    echo "  !! $d 拉取失败(rebase 冲突或 untracked 撞名?)——本仓本轮跳过,处理后重跑"
    continue
  fi
  git -C "$ROOT/$d" add -A
  if git -C "$ROOT/$d" diff --cached --quiet; then
    echo "  (无本地改动)"
  else
    git -C "$ROOT/$d" commit -m "$MSG"
  fi
  # 单仓推送失败(网络瞬断等)不连坐另一仓,重跑即可续上。
  git -C "$ROOT/$d" push || echo "  !! $d 推送失败(网络?),稍后重跑 sync.sh 续上"
done
