# -*- coding: utf-8 -*-
"""
zurg_guard 纯逻辑单测。要锁死的性质:
  · RD 限流 / 5xx / 网络错 = 没测到,绝不能据此清标记或重启
  · 只清 RD 验证过能读的文件;记录里还有坏文件时 Unfixable 不清
  · 有人在用、冷却中、超每日上限都不许重启
  · 日志里的 rclone 读失败能被摘出来,容器里 docker exec 起的脚本能被认出来
跑法:python3 tests/test_zurg_guard.py(失败时非零退出)
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "..", "tools", "zurg_guard", "zurg_guard.py")
if not os.path.exists(TARGET):
    raise SystemExit("找不到被测模块 %s" % TARGET)
print("被测模块: %s" % os.path.normpath(TARGET))
spec = importlib.util.spec_from_file_location("zg", TARGET)
zg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zg)

fails = []


def check(name, got, want):
    if got != want:
        fails.append("%s: 得到 %r,应为 %r" % (name, got, want))
    else:
        print("ok  %s" % name)


# ---- range_for
check("大文件跳过头部读 1MiB 处", zg.range_for(10 << 30), (1 << 20, (1 << 20) + 65535))
check("小文件从头读到尾", zg.range_for(1000), (0, 999))
check("大小未知从头读 64KiB", zg.range_for(None), (0, 65535))

# ---- classify_rd:最要命的是"没测到"不能被当成结论
E = 65536
check("206 读满 = ok", zg.classify_rd(200, None, 206, E, E)[0], "ok")
check("200 读满 = ok", zg.classify_rd(200, None, 200, E, E)[0], "ok")
check("451 = dead", zg.classify_rd(451, "infringing_file", None, 0, E)[0], "dead")
check("429 = untested", zg.classify_rd(429, "too_many_requests", None, 0, E)[0], "untested")
check("RD 503 = untested", zg.classify_rd(503, None, None, 0, E)[0], "untested")
check("hoster_unavailable 503 = rdfail(RD 侧,不重启 zurg)", zg.classify_rd(503, "hoster_unavailable", None, 0, E)[0], "rdfail")
check("网络错 = untested", zg.classify_rd(None, "URLError", None, 0, E)[0], "untested")
check("下载地址超时 = untested", zg.classify_rd(200, None, None, 0, E)[0], "untested")
check("下载地址 5xx = untested", zg.classify_rd(200, None, 502, 0, E)[0], "untested")
check("下载地址 404 = rdfail", zg.classify_rd(200, None, 404, 0, E)[0], "rdfail")
check("只读到一半 = untested", zg.classify_rd(200, None, 206, 100, E)[0], "untested")
check("hoster_not_free 403 = rdfail(不是令牌问题)", zg.classify_rd(403, "hoster_not_free", None, 0, E)[0], "rdfail")
check("bad_token 401 = auth", zg.classify_rd(401, "bad_token", None, 0, E)[0], "auth")
check("unavailable_file 503 = rdfail(RD 上文件没了,不重启 zurg)", zg.classify_rd(503, "unavailable_file", None, 0, E)[0], "rdfail")

# ---- needs_attention / files_to_verify / unflag
def rec(files, unfixable=""):
    return {"SelectedFiles": {k: dict({"selected": 1, "IsDeleted": False, "Link": "L" + k, "bytes": 10}, **v)
                              for k, v in files.items()},
            "Unfixable": unfixable, "Name": "T"}

healthy = rec({"a": {"IsBroken": False}})
check("健康记录不用管", zg.needs_attention(healthy), False)
locked = rec({"a": {"IsBroken": True}}, "infringing torrent")
check("误锁记录要管", zg.needs_attention(locked), True)
only_unfix = rec({"a": {"IsBroken": False}, "b": {"IsBroken": False}}, "infringing torrent")
check("只有 Unfixable 也要管", zg.needs_attention(only_unfix), True)
check("只有 Unfixable 时整条都验", sorted(k for k, _ in zg.files_to_verify(only_unfix)), ["a", "b"])
deleted_broken = rec({"a": {"IsBroken": True, "IsDeleted": True}})
check("已删的坏文件不算", zg.needs_attention(deleted_broken), False)

new, changed = zg.unflag(locked, {"a"})
check("单文件验过能读:清 IsBroken", new["SelectedFiles"]["a"]["IsBroken"], False)
check("单文件验过能读:清 Unfixable", new["Unfixable"], "")
check("单文件验过能读:有改动", changed, True)
check("unflag 不改原对象", locked["SelectedFiles"]["a"]["IsBroken"], True)

two = rec({"a": {"IsBroken": True}, "b": {"IsBroken": True}}, "infringing torrent")
new, changed = zg.unflag(two, {"a"})
check("部分能读:只清能读的", (new["SelectedFiles"]["a"]["IsBroken"], new["SelectedFiles"]["b"]["IsBroken"]), (False, True))
check("部分能读:Unfixable 保留", new["Unfixable"], "infringing torrent")

new, changed = zg.unflag(only_unfix, {"a"})
check("只有 Unfixable 但只验过一半:不清", (new["Unfixable"], changed), ("infringing torrent", False))
new, changed = zg.unflag(only_unfix, {"a", "b"})
check("只有 Unfixable 且全验过:清", (new["Unfixable"], changed), ("", True))

new, changed = zg.unflag(locked, set())
check("一个都没验过:不动", changed, False)

# ---- may_restart
NOW = 1_000_000
check("空闲且没重启过:可以", zg.may_restart(NOW, [], [])[0], True)
check("有人在看:不行", zg.may_restart(NOW, [], ["Plex 正在播放 1 路"])[0], False)
check("10 分钟前刚重启:冷却中", zg.may_restart(NOW, [NOW - 600], [])[0], False)
check("25 分钟前重启:可以", zg.may_restart(NOW, [NOW - 1500], [])[0], True)
six = [NOW - 3600 * i for i in range(1, 7)]
check("24 小时内已 6 次:不行", zg.may_restart(NOW, six, [])[0], False)
old = [NOW - 86400 - 3600 * i for i in range(1, 7)]
check("6 次都在 24 小时前:可以", zg.may_restart(NOW, old, [])[0], True)

# ---- parse_rclone_errors(真实日志行)
LOGTXT = '''Nov 15, 2026 06:09:20 - ERROR - rclone mount name "pd_zurg": movies/The.Prestige.2006.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-FGT/The.Prestige.2006.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-FGT.mkv: vfs cache: failed to download: vfs reader: failed to write to cache file: File is temporarily unavailable: 423 Locked
Nov 15, 2026 06:09:20 - ERROR - rclone mount name "pd_zurg": movies/The.Prestige.2006.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-FGT/The.Prestige.2006.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-FGT.mkv: vfs cache: too many errors 11/10
Nov 15, 2026 06:09:20 - ERROR - rclone mount name "pd_zurg": IO error: vfs reader: failed to write to cache file: File is temporarily unavailable: 423 Locked
Nov 15, 2026 06:05:02 - ERROR - rclone mount name "pd_zurg": movies/The Odyssey 2026 1080p/The Odyssey 2026 1080p.mkv: vfs cache: failed to _ensure cache vfs reader
Nov 15, 2026 06:05:05 - INFO - Zurg w/ RealDebrid subprocess: manager Successfully repaired torrent The Odyssey 2026 1080p (files=1) by redownloading
'''
check("摘出 rclone 读失败文件并去重", zg.parse_rclone_errors(LOGTXT), [
    "movies/The.Prestige.2006.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-FGT/The.Prestige.2006.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-FGT.mkv",
    "movies/The Odyssey 2026 1080p/The Odyssey 2026 1080p.mkv"])

# ---- container_scripts / mount_readers
TOP = '''PID                 COMMAND
2101                /venv/bin/python3 /tmp/rd_autoheal.py --apply --limit 3
2200                rclone mount pd_zurg: /data/pd_zurg
2301                python3 /tmp/watchlist_prune.py --apply
'''
check("认出容器里的 docker exec 脚本", zg.container_scripts(TOP), ["rd_autoheal.py", "watchlist_prune.py"])
check("容器里只有常驻进程时为空", zg.container_scripts("PID COMMAND\n1 python main.py\n2 rclone mount"), [])
check("认出宿主机读挂载的 ffmpeg", zg.mount_readers([["/usr/bin/ffmpeg", "-i", "/volume1/docker/pd_zurg/mnt/pd_zurg/movies/x.mkv"]]), ["ffmpeg"])
check("不读挂载的 ffmpeg 不算", zg.mount_readers([["/usr/bin/ffmpeg", "-i", "/tmp/a.mkv"]]), [])

# ---- pick_batch
files = ["a", "b", "c", "d", "e"]
check("从游标取并前进", zg.pick_batch(files, 1, 2, []), (["b", "c"], 3))
check("取到末尾回绕", zg.pick_batch(files, 4, 3, []), (["e", "a", "b"], 2))
check("报错的文件排最前且不重复", zg.pick_batch(files, 0, 2, ["c"]), (["c", "a", "b"], 2))
check("n 大于总数时每个只取一次", zg.pick_batch(files, 0, 99, [])[0], files)

# ---- repair_setting
check("enable_repair: false", zg.repair_setting("zurg: v1\nenable_repair: false\n"), "false")
check("enable_repair: true", zg.repair_setting("enable_repair: true # x"), "true")
check("没写 = 默认开", zg.repair_setting("zurg: v1\n"), "default(true)")
check("注释掉的不算", zg.repair_setting("# enable_repair: false\n"), "default(true)")

if fails:
    print("\n%d 条失败:" % len(fails))
    for f in fails:
        print("  ✗ " + f)
    sys.exit(1)
print("\n全部通过")
