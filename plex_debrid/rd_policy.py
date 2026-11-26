# -*- coding: utf-8 -*-
"""Real-Debrid addMagnet 的规矩 —— 只此一份,plex_debrid(debrid/services/realdebrid.py)与 tools/autoheal 共用。

RD 对 addMagnet 回 `451 infringing_file`(error_code 35)有三种原因,回包一模一样
(DMM notes.debridmediamanager.com/providers/realdebrid;2026-11-24 在本账号实测吻合):
  1. 种子名命中 RD 的拒收串(REFUSED_*),加了必拒 —— 加之前就跳过,省掉一个加种间隔;
  2. 同账号两次加种不足 20 秒 —— 限流,且有记忆(触发后一阵子什么都拒,重试会续命)。
     所以**所有**加种进程走同一个跨进程节拍 wait_turn(),整个账号两次加种至少隔 ADD_INTERVAL;
  3. 真的按 hash 被版权下架 —— 只能换下一个候选。
11-24 的事故:plex_debrid 7.5 秒一个,每批第三个起全被限流,却当"侵权"跳过,新片整批落空进冷却。
"""
import os
import re
import sys
import time

try:
    import fcntl
except ImportError:  # 非 POSIX(本地单测机)退回进程内节拍
    fcntl = None

ADD_INTERVAL = 21.0  # 秒;RD 门槛 20 秒,多留 1 秒余量
STAMP_PATH = os.environ.get("RD_ADD_STAMP", "/config/rd_last_add")  # 上一次加种的时间戳,跨进程共享

# 拒收串(DMM 2026-10-14 版,共 11 个)。分隔符算数:`WEB.DL`、`WEB-x264`、`BluRay-x264` 都放行。
REFUSED_ANYCASE = ("WEBRip", "BDRip", "HDRip", "DVDRip", "HDTV.x264", "HDTV.XviD", "BluRay.x264", "p.BluRay.DTS")
REFUSED_EXACT = ("WEB-DL", "WEB.x264", "WEB.H264")  # 只在大小写完全一致时拒
_anycase = re.compile("|".join(re.escape(s) for s in REFUSED_ANYCASE), re.I)
_exact = re.compile("|".join(re.escape(s) for s in REFUSED_EXACT))

_last = [0.0]      # 节拍文件用不了时的进程内兜底
_warned = [False]


def refused_name(title):
    """种子名命中 RD 拒收串时返回命中的那段文字,否则返回 None。"""
    title = title or ""
    m = _anycase.search(title) or _exact.search(title)
    return m.group(0) if m else None


def _warn(msg):
    if not _warned[0]:
        _warned[0] = True
        print("[rd_policy] %s" % msg, file=sys.stderr, flush=True)


def wait_turn():
    """排队:距上一次加种(任何进程)不足 ADD_INTERVAL 就睡够,再记下这一次。返回实际睡了多少秒。

    持文件锁期间睡,所以多个进程按先来后到依次加,不会两个同时算出"已经够久了"。
    节拍文件打不开就退回进程内节拍,并在 stderr 说一次(不静默)。
    """
    fd = None
    last = _last[0]
    if fcntl is not None:
        try:
            fd = os.open(STAMP_PATH, os.O_RDWR | os.O_CREAT, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = os.read(fd, 64).decode("ascii", "ignore").strip()
            if raw:
                last = max(last, float(raw))
        except (OSError, ValueError) as e:
            _warn("节拍文件 %s 用不了(%s: %s),只在本进程内节流,别的加种进程可能撞上 RD 20 秒限流"
                  % (STAMP_PATH, type(e).__name__, e))
            if fd is not None:
                os.close(fd)
                fd = None
    try:
        delay = last + ADD_INTERVAL - time.time()
        if delay > 0:
            time.sleep(delay)
        now = time.time()
        _last[0] = now
        if fd is not None:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, ("%.3f" % now).encode("ascii"))
        return max(delay, 0.0)
    finally:
        if fd is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
