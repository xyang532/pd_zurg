#!/bin/sh
# 由 DSM 计划任务(root)定时调用 —— 把自愈工具拷进 pd_zurg 容器并执行。
#
# 为什么要这层包装:工具本身必须跑在容器里(要用 plex_debrid 的抓取器和版本档),
# 而 patches/ 没有 bind-mount 进容器(补丁已烤进镜像,不再挂载),所以每次先 cp 再 exec。
# 这样容器重建后也不会丢 —— 权威副本一直在 NAS 的 patches/ 下。
#
# 日志:容器里的 /log 就是 /volume1/docker/pd_zurg/log,所以 autoheal.log 在 NAS 上直接可读。
# 无事发生的一轮不写任何东西(工具内部按状态变化决定)。
set -u

DOCKER=/usr/local/bin/docker
[ -x "$DOCKER" ] || DOCKER=/var/packages/ContainerManager/target/usr/bin/docker
SRC=/volume1/docker/pd_zurg/pd_zurg/tools/autoheal/rd_autoheal.py
LOG=/volume1/docker/pd_zurg/log/autoheal.log

# 容器里没设 TZ,走 UTC;宿主是 PDT。同一个日志文件里混两种时区会误导排查,统一按 UTC 记。
stamp() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

if [ ! -x "$DOCKER" ]; then
  echo "$(stamp) ABORT   找不到 docker" >> "$LOG"
  exit 1
fi
# 分档记录:"docker 用不了" 和 "容器没跑" 是两回事,混成一档会把权限问题读成"今晚没事"
PS_OUT=$("$DOCKER" ps --format '{{.Names}}' 2>&1)
if [ $? -ne 0 ]; then
  # 群晖的 /bin/sh 是 ash,不能用 $'\n' 这种 bash 扩展
  echo "$(stamp) ABORT   docker ps 失败(多半是没以 root 运行): $(echo "$PS_OUT" | head -1)" >> "$LOG"
  exit 1
fi
if ! echo "$PS_OUT" | grep -qx pd_zurg; then
  echo "$(stamp) SKIP    pd_zurg 未运行,本轮跳过" >> "$LOG"
  exit 0
fi
if [ ! -f "$SRC" ]; then
  echo "$(stamp) ABORT   找不到 $SRC" >> "$LOG"
  exit 1
fi

"$DOCKER" cp "$SRC" pd_zurg:/tmp/rd_autoheal.py >/dev/null 2>&1 || {
  echo "$(stamp) ABORT   docker cp 失败" >> "$LOG"
  exit 1
}

# 默认全自动替换,单轮最多 3 个(防止某天大面积下架时跑飞)。
# 想只看不动手就传 --scan-only,想改上限就传 --limit N。
if [ $# -eq 0 ]; then
  set -- --apply --limit 3
fi
exec "$DOCKER" exec pd_zurg /venv/bin/python3 /tmp/rd_autoheal.py "$@"
