#!/bin/sh
# **手动跑的批量补齐工具,已不再由计划任务调用。**
# 日常的"新片入库自动配字幕"改由 subs_hook.py 接 Plex 的 library.new 事件完成 ——
# 用户 2026-08-03 明确:老片不要动,只管新进来的,而定时任务天然是扫全库。
# 保留本脚本是为了偶尔手动补一批(例如想把库里已有的字幕统一对一遍轴时)。
#
# 为什么是四步、为什么是这个顺序:
#   ① fix_subs  --mode missing  走 Plex 自带的 OpenSubtitles,**不花外部配额**,先榨干它;
#   ② fetch_subs                走 assrt,补 OpenSubtitles 中文覆盖不到的(老片/华语/冷门),
#                               assrt 是 20 次/分钟的速率限制,工具内部已按 3.2 秒节流;
#   ③ align_subs                拿片源自带字幕轨当时间轴真值,把错位的改写对齐;
#   ④ fix_select                兜底:有字幕却没被选中的补选上(前几步删流时可能留下这种)。
#
# ①②④ 必须在容器里跑(要 /config/settings.json 和容器网络里的 plex 主机名),
# ③ 必须在宿主机上跑(要宿主机的 ffmpeg 和挂载盘,容器里两样都没有)。
#
# **不跑 dedup**:它的策略是"留最旧的"(为了保护用户手工上传的那几条),而 align 刚把
# 对齐后的新流传上去、旧流删掉 —— 万一删除那一步失败,dedup 会反过来把对齐好的那条删掉。
#
# 日志统一写 /log/subs.log(= 宿主机 /volume1/docker/pd_zurg/log/subs.log)。
set -u

DOCKER=/usr/local/bin/docker
[ -x "$DOCKER" ] || DOCKER=/var/packages/ContainerManager/target/usr/bin/docker
TOOLS=/volume1/docker/pd_zurg/pd_zurg/tools/subs
LOG=/volume1/docker/pd_zurg/log/subs.log

# 容器里没设 TZ,走 UTC;宿主是 PDT。同一个日志文件混两种时区会误导排查,统一按 UTC。
stamp() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

# 单轮上限。三个数的约束完全不同,所以分开给,别合成一个:
#   PLEX_N  受 Plex 挂载字幕的往返延迟限制(每个候选要等它生效)
#   ASSRT_N 受 assrt 20 次/分钟的速率限制
#   ALIGN_N 受**读盘带宽**限制(实测 34.4 MB/s,每部片要读两个 300 秒窗口,约 90 秒 / 2.4 GB)
PLEX_N=${PLEX_N:-10}
ASSRT_N=${ASSRT_N:-10}
ALIGN_N=${ALIGN_N:-12}

if [ ! -x "$DOCKER" ]; then
  echo "$(stamp) ABORT    找不到 docker" >> "$LOG"
  exit 1
fi
# 分档记录:"docker 用不了" 和 "容器没跑" 是两回事,混成一档会把权限问题读成"今晚没事"
PS_OUT=$("$DOCKER" ps --format '{{.Names}}' 2>&1)
if [ $? -ne 0 ]; then
  echo "$(stamp) ABORT    docker ps 失败(多半是没以 root 运行): $(echo "$PS_OUT" | head -1)" >> "$LOG"
  exit 1
fi
if ! echo "$PS_OUT" | grep -qx pd_zurg; then
  echo "$(stamp) SKIP     pd_zurg 未运行,本轮跳过" >> "$LOG"
  exit 0
fi

for f in fix_subs.py fetch_subs.py fix_select.py align_subs.py; do
  if [ ! -f "$TOOLS/$f" ]; then
    echo "$(stamp) ABORT    找不到 $TOOLS/$f" >> "$LOG"
    exit 1
  fi
done

run_in_container() {
  _f=$1; shift
  "$DOCKER" cp "$TOOLS/$_f" "pd_zurg:/tmp/$_f" >/dev/null 2>&1 || {
    echo "$(stamp) ABORT    docker cp $_f 失败" >> "$LOG"
    return 1
  }
  "$DOCKER" exec pd_zurg /venv/bin/python3 "/tmp/$_f" "$@"
}

# 每步各自失败各自记,不互相拖累 —— 前一步没配额了不该让后一步也不跑。
run_in_container fix_subs.py --mode missing --apply --limit "$PLEX_N" \
  || echo "$(stamp) WARN     fix_subs 非零退出" >> "$LOG"
run_in_container fetch_subs.py --apply --limit "$ASSRT_N" \
  || echo "$(stamp) WARN     fetch_subs 非零退出" >> "$LOG"
/usr/bin/python3 "$TOOLS/align_subs.py" --apply --limit "$ALIGN_N" \
  || echo "$(stamp) WARN     align_subs 非零退出" >> "$LOG"
run_in_container fix_select.py --apply \
  || echo "$(stamp) WARN     fix_select 非零退出" >> "$LOG"

exit 0
