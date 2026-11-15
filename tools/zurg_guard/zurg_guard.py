#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zurg 守护:接替 zurg 自带的修复(已用 /config/config.yml 关掉 enable_repair),
任何动作之前都先用 Real-Debrid 直连验证过。

为什么(2026-11-14 实测):zurg v0.9.3 的自带修复会把一次偶发错误变成永久故障 ——
  偶发 404 → 修复 = 重新提交磁力 → RD 对新提交回 451 → zurg 在 RD/data/<id>.json 落盘
  Unfixable="infringing torrent" + IsBroken=true → 之后永远回 423,重启也不清,
  而 RD 上那份旧缓存一直能读。全库 45 条被这样锁住,39 条其实能读。
  另一类是 zurg 进程内状态坏掉(404 修复循环),数据都对但就是读不出,重启即好(10-01、11-14 各一次)。

每轮(DSM 计划任务每 10 分钟,root)做三件事:
  1. 扫 RD/data/*.json 里带 IsBroken / Unfixable 的记录,逐个用 RD 直连验(unrestrict + Range 读 64 KiB):
     读得到 = zurg 误判 → 清标记(先备份);读不到 = RD 侧死档,归 autoheal 管,这里只记一次。
  2. 经 zurg 的 WebDAV 抽查片库:先查日志里刚报过读取失败的文件,再按游标轮流查,每轮 --probe 个。
     zurg 读不到而 RD 直连读得到 = zurg 侧故障 → 需要重启 zurg。
  3. 需要清标记或重启时,先确认没人在用(Plex 无播放、容器里没有 autoheal 等脚本、宿主机没有 ffmpeg 在读挂载),
     再 stop → 改 json → start → 复验;有人在用就推迟到下一轮。
     重启有冷却和每日上限;同一个文件重启两次还读不出就停手,记 STUCK 等人看。

日志 log/zurg_guard.log,只记动作和状态变化,时间一律 UTC(与 autoheal.log 一致)。
默认干跑只报告;--apply 才动手。状态存 config/zurg_guard_state.json,改记录前的备份在 log/data/zurg_guard_backup/<日期>/。

部署(宿主机,root):
  1. cp tools/zurg_guard/zurg_config.override.yml /volume1/docker/pd_zurg/config/config.yml
     (pd_zurg 每次启动拿它覆盖 zurg 配置;唯一改动是 enable_repair: false。下次重启 pd_zurg 生效,本守护第一次动手时就会重启)
  2. DSM 计划任务 id=7「pd_zurg zurg_guard」,每天 0:00–23:50 每 10 分钟,root,命令先找主目录的本文件、找不到再找
     pd_zurg-guard 工作目录;/etc/crontab 有对应的 synoschedtask --run id=7 行。
  手动跑:sudo python3 tools/zurg_guard/zurg_guard.py            (干跑)
          sudo /usr/syno/bin/synoschedtask --run id=7 check_time=0 (走计划任务入口,实弹)
"""
import argparse
import copy
import datetime
import fcntl
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------- 路径与常量(唯一一份)
BASE = "/volume1/docker/pd_zurg"
DATA_DIR = BASE + "/RD/data"                 # = 容器里的 /zurg/RD/data(bind-mount)
ZURG_LIVE_CONFIG = BASE + "/RD/config.yml"   # zurg 实际在用的配置(每次启动由 setup.py 生成)
ZURG_OVERRIDE = BASE + "/config/config.yml"  # 存在时 setup.py 每次启动拿它覆盖上面那份
MOUNT = BASE + "/mnt/pd_zurg"                # rclone 挂载在宿主机上的位置
COMPOSE = BASE + "/compose.yaml"
LOG = BASE + "/log/zurg_guard.log"
STATE = BASE + "/config/zurg_guard_state.json"
BACKUP_DIR = BASE + "/log/data/zurg_guard_backup"
PDZURG_LOG_GLOB = BASE + "/log/PDZURG-*.log"
LOCK = "/tmp/zurg_guard.lock"
PLEX_PREFS = ("/volume1/docker/plex/library/Library/Application Support/"
              "Plex Media Server/Preferences.xml")
DAV = "http://127.0.0.1:8800/dav"
PLEX = "http://127.0.0.1:32400"
RD_API = "https://api.real-debrid.com/rest/1.0"
CONTAINER = "pd_zurg"
SECTIONS = ("movies", "shows", "anime")
VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m2ts", ".ts", ".mov", ".wmv", ".m4v", ".mpg", ".mpeg", ".webm")

PROBE_BYTES = 65536
PROBE_OFFSET = 1 << 20          # 跳过文件头(头部可能被 rclone/Plex 缓存过,测不出真实状态)
RD_MIN_INTERVAL = 1.0           # RD unrestrict 1.5 次/秒会大面积 429,1.0 次/秒稳(AGENT_NOTES §rd_autoheal)
RESTART_COOLDOWN = 20 * 60      # 两次重启至少隔 20 分钟
RESTART_DAILY_CAP = 6           # 24 小时最多重启 6 次
RD_DEAD_RECHECK = 6 * 3600      # RD 侧已确认读不了的记录,6 小时才再验一次(省 RD 调用)
STUCK_AFTER_RESTARTS = 2        # 同一文件重启两次还读不出 → 停手报警
LISTING_TTL = 3600              # 片库文件清单一小时刷新一次
READY_TIMEOUT = 180             # 重启后等 zurg + 挂载就绪的上限
DOCKER_CANDIDATES = ("/usr/local/bin/docker", "/var/packages/ContainerManager/target/usr/bin/docker")

RCLONE_ERR = re.compile(r'rclone mount name "[^"]+": (?P<rel>(?:%s)/[^:]+?): vfs cache' % "|".join(SECTIONS))
CONTAINER_SCRIPT = re.compile(r"python3?\s+/tmp/(\S+\.py)")


# ---------------------------------------------------------------- 纯逻辑(单测覆盖)
def range_for(size):
    """读哪一段:大文件跳过头部读 1 MiB 处的 64 KiB,小文件从头读,不越过文件尾。"""
    if size is None or size <= 0:
        return 0, PROBE_BYTES - 1
    start = PROBE_OFFSET if size > PROBE_OFFSET + PROBE_BYTES else 0
    end = min(start + PROBE_BYTES, size) - 1
    return start, end


def classify_rd(unrestrict_status, rd_error, dl_status, nbytes, expected):
    """RD 直连验证结果分档。返回 (档, 说明):
       ok       = RD 能读,zurg 那边读不出就是 zurg 的错
       dead     = RD 永久不给(451 侵权)→ autoheal 的事
       rdfail   = RD 现在给不了(文件没了/hoster 挂了/下载地址 4xx)→ 也不是 zurg 的错,不重启
       untested = 限流/5xx/网络错 → 等于没测,绝不能据此动手
       auth     = 令牌不对 → 整轮作废
    """
    if unrestrict_status in (401, 403) and rd_error in ("bad_token", "permission_denied", "account_locked", None):
        return "auth", "RD 令牌被拒(%s %s)" % (unrestrict_status, rd_error)
    if unrestrict_status == 451 or rd_error == "infringing_file":
        return "dead", "RD 451 infringing_file"
    if unrestrict_status == 429 or rd_error == "too_many_requests":
        return "untested", "RD 限流 429"
    if unrestrict_status is None:
        return "untested", "RD 连不上(%s)" % (rd_error or "网络错")
    if unrestrict_status >= 500 and rd_error not in ("hoster_unavailable", "unavailable_file"):
        return "untested", "RD %s %s" % (unrestrict_status, rd_error or "")
    if unrestrict_status != 200:
        return "rdfail", "RD unrestrict %s %s" % (unrestrict_status, rd_error or "")
    if dl_status is None:
        return "untested", "RD 下载地址连不上"
    if dl_status >= 500:
        return "untested", "RD 下载地址 %s" % dl_status
    if dl_status in (200, 206) and nbytes == expected:
        return "ok", "RD 直连 %s 读满 %d 字节" % (dl_status, nbytes)
    if dl_status in (200, 206):
        return "untested", "RD 直连只读到 %d/%d 字节" % (nbytes, expected)
    return "rdfail", "RD 下载地址 %s" % dl_status


def probe_ok(status, nbytes, expected):
    return status in (200, 206) and nbytes == expected


def live_files(rec):
    """记录里还在用的文件(selected 且没删)。"""
    out = []
    for key, f in (rec.get("SelectedFiles") or {}).items():
        if f.get("IsDeleted"):
            continue
        if f.get("selected", 1) != 1:
            continue
        out.append((key, f))
    return out


def needs_attention(rec):
    return bool(rec.get("Unfixable")) or any(f.get("IsBroken") for _, f in live_files(rec))


def files_to_verify(rec):
    """要拿去 RD 验的文件:标了 IsBroken 的;只有 Unfixable 没有 IsBroken 时,整条记录的文件都验。"""
    files = live_files(rec)
    broken = [(k, f) for k, f in files if f.get("IsBroken")]
    return broken if broken else (files if rec.get("Unfixable") else [])


def unflag(rec, readable_keys):
    """只清 RD 验证过能读的那些文件的 IsBroken;所有在用文件都不再 broken 时才清 Unfixable。
       返回 (新记录, 是否有改动)。不改原对象。"""
    new = copy.deepcopy(rec)
    changed = False
    for key, f in (new.get("SelectedFiles") or {}).items():
        if key in readable_keys and f.get("IsBroken"):
            f["IsBroken"] = False
            changed = True
    still_broken = any(f.get("IsBroken") for _, f in live_files(new))
    verified_all = all(k in readable_keys for k, _ in files_to_verify(rec))
    if new.get("Unfixable") and not still_broken and verified_all:
        new["Unfixable"] = ""
        changed = True
    return new, changed


def may_restart(now, history, busy):
    """history = 以往重启的 epoch 秒。返回 (能否重启, 原因)。"""
    if busy:
        return False, "有人在用:" + ";".join(busy)
    recent = [t for t in history if now - t < 86400]
    if recent and now - max(recent) < RESTART_COOLDOWN:
        return False, "冷却中(上次重启 %d 分钟前,至少隔 %d 分钟)" % ((now - max(recent)) // 60, RESTART_COOLDOWN // 60)
    if len(recent) >= RESTART_DAILY_CAP:
        return False, "24 小时内已重启 %d 次(上限 %d)" % (len(recent), RESTART_DAILY_CAP)
    return True, ""


def parse_rclone_errors(text):
    """从 pd_zurg 日志里摘出 rclone 读失败的文件(相对挂载根的路径),去重保序。"""
    seen, out = set(), []
    for m in RCLONE_ERR.finditer(text):
        rel = m.group("rel")
        if rel not in seen:
            seen.add(rel)
            out.append(rel)
    return out


def container_scripts(top_output):
    """docker top 输出里由 docker exec 起的 /tmp/*.py(autoheal、watchlist_prune 等)。重启会把它们连根拔掉。"""
    return sorted(set(CONTAINER_SCRIPT.findall(top_output or "")))


def mount_readers(cmdlines):
    """宿主机上正在读挂载的 ffmpeg/ffprobe(字幕对轴等)。cmdlines = 每个进程的参数列表。"""
    out = []
    for argv in cmdlines:
        if not argv:
            continue
        prog = os.path.basename(argv[0])
        if prog not in ("ffmpeg", "ffprobe"):
            continue
        joined = " ".join(argv)
        if "/pd_zurg/mnt/" in joined or "/data/pd_zurg" in joined or "/rclone/" in joined:
            out.append(prog)
    return out


def pick_batch(files, cursor, n, priority):
    """本轮抽查哪些:priority(日志里刚报错的)排最前,再从游标处轮流取,取到末尾回绕。返回 (列表, 新游标)。"""
    batch, seen = [], set()
    for rel in priority:
        if rel not in seen:
            batch.append(rel)
            seen.add(rel)
    if not files or n <= 0:
        return batch, cursor
    cursor = cursor % len(files)
    taken = 0
    i = cursor
    while taken < min(n, len(files)):
        rel = files[i]
        if rel not in seen:
            batch.append(rel)
            seen.add(rel)
        taken += 1
        i = (i + 1) % len(files)
    return batch, i


def repair_setting(config_text):
    """zurg 配置里 enable_repair 的值;没写时 zurg 默认是开的。"""
    m = re.search(r"^\s*enable_repair\s*:\s*(\S+)", config_text or "", re.M)
    return (m.group(1).lower() if m else "default(true)")


# ---------------------------------------------------------------- 运行环境
class Guard(object):
    def __init__(self, args):
        self.args = args
        self.state = self._load_state()
        self.docker = next((d for d in DOCKER_CANDIDATES if os.access(d, os.X_OK)), None)
        self._rd_key = None
        self._rd_last = 0.0
        self._plex_token = None
        self.events = 0

    # ---- 日志 / 状态
    @staticmethod
    def stamp():
        return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    def log(self, kind, msg):
        line = "%s %-8s %s" % (self.stamp(), kind, msg)
        print(line)
        self.events += 1
        if not self.args.apply and kind not in ("ABORT",):
            return  # 干跑不写日志,只打到屏幕
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def once(self, key, value, kind, msg):
        """同一个 key 的值没变就不重复记(只记状态变化)。"""
        seen = self.state.setdefault("seen", {})
        if seen.get(key) == value:
            return
        seen[key] = value
        self.log(kind, msg)

    def forget(self, key):
        self.state.setdefault("seen", {}).pop(key, None)

    @staticmethod
    def _load_state():
        try:
            with open(STATE, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def save_state(self):
        if not self.args.apply:
            return
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE)

    # ---- docker
    def docker_run(self, *argv, timeout=120):
        p = subprocess.run([self.docker] + list(argv), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout, universal_newlines=True)
        return p.returncode, p.stdout

    def container_running(self):
        rc, out = self.docker_run("ps", "--format", "{{.Names}}")
        if rc != 0:
            return None, out.strip().splitlines()[0] if out.strip() else "docker ps 失败"
        return CONTAINER in out.split(), ""

    # ---- RD
    def rd_key(self):
        if self._rd_key is None:
            with open(COMPOSE, encoding="utf-8") as fh:
                for line in fh:
                    m = re.search(r"RD_API_KEY\s*[=:]\s*\"?([A-Za-z0-9]{20,})", line)
                    if m and not line.lstrip().startswith("#"):
                        self._rd_key = m.group(1)
                        break
        if not self._rd_key:
            raise RuntimeError("compose.yaml 里找不到 RD_API_KEY")
        return self._rd_key

    def _rd_throttle(self):
        wait = RD_MIN_INTERVAL - (time.time() - self._rd_last)
        if wait > 0:
            time.sleep(wait)
        self._rd_last = time.time()

    def rd_check(self, link, size):
        """RD 直连验一个文件:unrestrict 拿下载地址,再 Range 读 64 KiB。返回 (档, 说明)。429 退避重试两次。"""
        start, end = range_for(size)
        expected = end - start + 1
        for attempt in range(3):
            self._rd_throttle()
            status, err, download = None, None, None
            try:
                req = urllib.request.Request(RD_API + "/unrestrict/link",
                                             data=urllib.parse.urlencode({"link": link}).encode(),
                                             headers={"Authorization": "Bearer " + self.rd_key()})
                with urllib.request.urlopen(req, timeout=30) as r:
                    status = r.status
                    download = json.load(r).get("download")
            except urllib.error.HTTPError as e:
                status = e.code
                try:
                    err = json.load(e).get("error")
                except Exception:
                    err = None
            except Exception as e:
                err = type(e).__name__
            if status == 429 and attempt < 2:
                time.sleep(15 * (attempt + 1))
                continue
            break
        dl_status, nbytes = None, 0
        if status == 200 and download:
            dl_status, nbytes = self.http_range(download, start, end)
        return classify_rd(status, err, dl_status, nbytes, expected)

    @staticmethod
    def http_range(url, start, end, timeout=30):
        req = urllib.request.Request(url, headers={"Range": "bytes=%d-%d" % (start, end)})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, len(r.read(end - start + 1))
        except urllib.error.HTTPError as e:
            return e.code, 0
        except Exception:
            return None, 0

    # ---- Plex / 忙闲
    def plex_token(self):
        if self._plex_token is None:
            with open(PLEX_PREFS, encoding="utf-8") as fh:
                m = re.search(r'PlexOnlineToken="([^"]+)"', fh.read())
            self._plex_token = m.group(1) if m else ""
        return self._plex_token

    def busy_reasons(self):
        reasons = []
        try:
            req = urllib.request.Request(PLEX + "/status/sessions",
                                         headers={"X-Plex-Token": self.plex_token(), "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                size = json.load(r).get("MediaContainer", {}).get("size", 0)
            if size:
                reasons.append("Plex 正在播放 %d 路" % size)
        except Exception as e:
            reasons.append("查不到 Plex 播放状态(%s),保守按有人在看" % type(e).__name__)
        rc, top = self.docker_run("top", CONTAINER, "-eo", "pid,args", timeout=30)
        if rc != 0:
            reasons.append("docker top 失败,不知道容器里有没有脚本在跑")
        else:
            scripts = container_scripts(top)
            if scripts:
                reasons.append("容器里在跑 " + ",".join(scripts))
        cmdlines = []
        for p in glob.glob("/proc/[0-9]*/cmdline"):
            try:
                with open(p, "rb") as fh:
                    cmdlines.append([a.decode("utf-8", "replace") for a in fh.read().split(b"\0") if a])
            except OSError:
                pass
        readers = mount_readers(cmdlines)
        if readers:
            reasons.append("宿主机 %s 正在读挂载" % ",".join(sorted(set(readers))))
        return reasons

    # ---- zurg 记录
    def load_records(self):
        recs = {}
        for path in glob.glob(DATA_DIR + "/*.json"):
            try:
                with open(path, encoding="utf-8") as fh:
                    recs[os.path.basename(path)[:-5]] = json.load(fh)
            except (OSError, ValueError) as e:
                self.once("badjson:" + path, "1", "WARN", "读不了 zurg 记录 %s(%s)" % (path, type(e).__name__))
        return recs

    @staticmethod
    def write_record(tid, rec):
        path = os.path.join(DATA_DIR, tid + ".json")
        day = datetime.datetime.utcnow().strftime("%Y%m%d")
        bdir = os.path.join(BACKUP_DIR, day)
        os.makedirs(bdir, exist_ok=True)
        bpath = os.path.join(bdir, tid + ".json")
        if not os.path.exists(bpath):
            shutil.copy2(path, bpath)
        st = os.stat(path)
        tmp = path + ".guardtmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, separators=(",", ":"))  # 与 zurg 自己写的紧凑格式一致
        os.chmod(tmp, st.st_mode & 0o7777)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except OSError:
            pass
        os.replace(tmp, path)

    # ---- 片库清单与抽查
    def listing(self):
        lst = self.state.get("listing") or {}
        if lst.get("files") and time.time() - lst.get("ts", 0) < LISTING_TTL:
            return lst["files"], lst.get("sizes", {})
        files, sizes = [], {}
        for sec in SECTIONS:
            root = os.path.join(MOUNT, sec)
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                depth = dirpath[len(MOUNT):].count(os.sep)
                if depth >= 3:
                    dirnames[:] = []
                for fn in filenames:
                    if fn.lower().endswith(VIDEO_EXT):
                        full = os.path.join(dirpath, fn)
                        rel = os.path.relpath(full, MOUNT)
                        try:
                            sizes[rel] = os.stat(full).st_size
                        except OSError:
                            continue
                        files.append(rel)
        files.sort()
        self.state["listing"] = {"ts": time.time(), "files": files, "sizes": sizes}
        return files, sizes

    def dav_probe(self, rel, size, timeout=40):
        start, end = range_for(size)
        url = DAV + "/" + urllib.parse.quote(rel)
        status, nbytes = self.http_range(url, start, end, timeout=timeout)
        return status, nbytes, end - start + 1

    def recent_failures(self):
        """上一轮之后 pd_zurg 日志里新出现的 rclone 读失败文件。"""
        logs = sorted(glob.glob(PDZURG_LOG_GLOB), key=os.path.getmtime)
        if not logs:
            return []
        cur = logs[-1]
        pos = self.state.get("logpos") or {}
        offset = pos.get("offset", 0) if pos.get("file") == cur else 0
        try:
            size = os.path.getsize(cur)
            if offset > size:
                offset = 0
            if not pos:  # 第一次运行:不回溯整份旧日志,只从现在开始看
                offset = max(0, size - 2_000_000)
            with open(cur, "rb") as fh:
                fh.seek(offset)
                text = fh.read().decode("utf-8", "replace")
                self.state["logpos"] = {"file": cur, "offset": fh.tell()}
        except OSError:
            return []
        return parse_rclone_errors(text)

    @staticmethod
    def index_by_name(recs):
        idx = {}
        for tid, rec in recs.items():
            for key, f in live_files(rec):
                name = os.path.basename(f.get("path") or key)
                idx.setdefault(name, []).append((tid, key, f.get("bytes")))
        return idx

    # ---- 重启
    def wait_ready(self):
        deadline = time.time() + READY_TIMEOUT
        while time.time() < deadline:
            time.sleep(5)
            try:
                req = urllib.request.Request(DAV + "/", method="PROPFIND", headers={"Depth": "1"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    dav_ok = r.status in (200, 207)
            except Exception:
                dav_ok = False
            try:
                mounted = len(os.listdir(os.path.join(MOUNT, "movies"))) > 0
            except OSError:
                mounted = False
            if dav_ok and mounted:
                time.sleep(10)  # zurg 起来后还要几秒拉完种子列表
                return True
        return False

    def stop_fix_start(self, fixes):
        """stop → 写记录 → start。start 放在 finally 里:无论中间出什么错,片库都不能一直停着。"""
        rc, out = self.docker_run("stop", CONTAINER, timeout=180)
        if rc != 0:
            self.log("ABORT", "docker stop %s 失败:%s" % (CONTAINER, out.strip()[:200]))
            return False
        try:
            for tid, rec in fixes.items():
                self.write_record(tid, rec)
        finally:
            for attempt in range(3):
                rc, out = self.docker_run("start", CONTAINER, timeout=180)
                if rc == 0:
                    break
                self.log("ABORT", "docker start %s 第 %d 次失败:%s" % (CONTAINER, attempt + 1, out.strip()[:200]))
                time.sleep(10)
        self.state.setdefault("restarts", []).append(int(time.time()))
        self.state["restarts"] = [t for t in self.state["restarts"] if time.time() - t < 86400 * 2]
        if rc != 0:
            return False
        if not self.wait_ready():
            self.log("ABORT", "重启后 %d 秒 zurg/挂载仍未就绪" % READY_TIMEOUT)
            return False
        return True

    # ---------------------------------------------------------------- 一轮
    def run(self):
        if not self.docker:
            self.log("ABORT", "找不到 docker")
            return 1
        running, why = self.container_running()
        if running is None:
            self.log("ABORT", "docker ps 失败(多半没以 root 运行):%s" % why)
            return 1
        if not running:
            self.once("container", "down", "SKIP", "%s 没在运行,本轮跳过" % CONTAINER)
            return 0
        self.forget("container")

        # 配置漂移:自带修复一旦被重新打开,误锁就会回来
        try:
            with open(ZURG_LIVE_CONFIG, encoding="utf-8") as fh:
                rep = repair_setting(fh.read())
        except OSError:
            rep = "读不到"
        if rep != "false":
            self.once("repair", rep, "WARN", "zurg 自带修复没关(enable_repair=%s)。检查 %s 是否还在,在的话重启 pd_zurg 让它生效"
                      % (rep, ZURG_OVERRIDE))
        else:
            self.once("repair", rep, "INFO", "zurg 自带修复已关(enable_repair=false)")

        recs = self.load_records()
        fixes, zurg_side, rd_side = {}, {}, {}

        # 1. 被 zurg 标了 IsBroken / Unfixable 的记录
        rdchk = self.state.setdefault("rdside_checked", {})
        for tid in list(rdchk):
            if tid not in recs or not needs_attention(recs[tid]):
                del rdchk[tid]
        for tid, rec in sorted(recs.items()):
            if not needs_attention(rec):
                self.forget("flag:" + tid)
                self.forget("found:" + tid)
                continue
            if time.time() - rdchk.get(tid, 0) < RD_DEAD_RECHECK:
                continue
            readable = set()
            for key, f in files_to_verify(rec):
                grade, detail = self.rd_check(f.get("Link", ""), f.get("bytes"))
                if grade == "auth":
                    self.log("ABORT", detail)
                    return 1
                if grade == "ok":
                    readable.add(key)
                elif grade in ("dead", "rdfail"):
                    rdchk[tid] = int(time.time())
                    self.once("flag:" + tid, grade + ":" + detail, "RDSIDE",
                              "%s 被 zurg 标坏,RD 也读不了(%s),%s" % (
                                  rec.get("Name", tid), detail,
                                  "RD 永久封了,归 autoheal 换源" if grade == "dead" else "RD 现在给不了,不是 zurg 的问题,不重启"))
                else:
                    self.once("flag:" + tid, "untested", "UNTESTED", "%s 被 zurg 标坏,这轮没测到(%s),下轮再验"
                              % (rec.get("Name", tid), detail))
            if readable:
                new, changed = unflag(rec, readable)
                if changed:
                    fixes[tid] = new
                    self.once("found:" + tid, "1", "FOUND", "%s:zurg 标了坏(Unfixable=%r),RD 直连能读 → 要清标记"
                              % (rec.get("Name", tid), rec.get("Unfixable", "")))

        # 2. 经 zurg 抽查片库
        files, sizes = self.listing()
        priority = [r for r in self.recent_failures() if r in sizes]
        batch, self.state["cursor"] = pick_batch(files, self.state.get("cursor", 0), self.args.probe, priority)
        idx = self.index_by_name(recs)
        stuck = self.state.setdefault("stuck", {})
        probed = failed = 0
        for rel in batch:
            status, nbytes, expected = self.dav_probe(rel, sizes.get(rel))
            probed += 1
            if probe_ok(status, nbytes, expected):
                if rel in stuck:
                    del stuck[rel]
                    self.log("RECOVER", "%s 经 zurg 又能读了" % rel)
                self.forget("probe:" + rel)
                self.forget("zside:" + rel)
                continue
            if status is None:
                continue  # 超时/连不上 = 没测到
            failed += 1
            cands = [c for c in idx.get(os.path.basename(rel), []) if c[2] == sizes.get(rel)] or \
                idx.get(os.path.basename(rel), [])
            if not cands:
                self.once("probe:" + rel, "unmapped", "WARN", "%s 经 zurg 读失败(%s),但在 zurg 记录里找不到对应种子" % (rel, status))
                continue
            tid, key, size = cands[0]
            grade, detail = self.rd_check(recs[tid]["SelectedFiles"][key].get("Link", ""), size)
            if grade == "auth":
                self.log("ABORT", detail)
                return 1
            if grade == "ok":
                if stuck.get(rel, 0) >= STUCK_AFTER_RESTARTS:
                    self.once("probe:" + rel, "stuck", "STUCK", "%s 经 zurg 读不出(%s)而 RD 能读,已重启 %d 次仍不好,停止为它重启,需要人看"
                              % (rel, status, stuck[rel]))
                    continue
                zurg_side[rel] = (status, sizes.get(rel))
                if needs_attention(recs[tid]) and tid not in fixes:
                    new, changed = unflag(recs[tid], {key})
                    if changed:
                        fixes[tid] = new
                self.once("zside:" + rel, str(status), "FOUND", "%s:经 zurg 读失败(%s),%s → zurg 侧故障" % (rel, status, detail))
            elif grade in ("dead", "rdfail"):
                rd_side[rel] = detail
                self.once("probe:" + rel, grade, "RDSIDE", "%s 经 zurg 读失败(%s),RD 也读不了(%s),归 autoheal" % (rel, status, detail))
            else:
                self.once("probe:" + rel, "untested", "UNTESTED", "%s 经 zurg 读失败(%s),RD 这轮没测到(%s)" % (rel, status, detail))

        self.state["last_run"] = self.stamp()
        day = self.stamp()[:10]
        daily = self.state.setdefault("daily", {})
        if daily.get("day") != day:
            if daily.get("day"):
                self.log("SUMMARY", "%s:抽查 %d 次、zurg 读失败 %d 次、清标记 %d 条、重启 %d 次、卡住 %d 个"
                         % (daily["day"], daily.get("probed", 0), daily.get("failed", 0), daily.get("fixed", 0),
                            daily.get("restarts", 0), len(stuck)))
            daily.clear()
            daily["day"] = day
        daily["probed"] = daily.get("probed", 0) + probed
        daily["failed"] = daily.get("failed", 0) + failed

        if not fixes and not zurg_side:
            self.forget("defer")
            return 0

        # 3. 动手:清标记 + 重启(一次重启同时生效)
        what = "清 %d 条标记、%d 个 zurg 侧读不出" % (len(fixes), len(zurg_side))
        if not self.args.apply:
            self.log("DRYRUN", "需要 " + what + ";加 --apply 才会 stop → 改记录 → start")
            return 0
        if self.args.no_restart:
            self.log("DRYRUN", "需要 " + what + ";--no-restart,本轮不动手")
            return 0
        ok, why = may_restart(time.time(), self.state.get("restarts", []), self.busy_reasons())
        if not ok:
            self.once("defer", why, "DEFER", "需要" + what + ",推迟:" + why)
            return 0
        self.forget("defer")
        self.log("RESTART", "重启 %s(%s)" % (CONTAINER, what))
        if not self.stop_fix_start(fixes):
            return 1
        daily["restarts"] = daily.get("restarts", 0) + 1
        daily["fixed"] = daily.get("fixed", 0) + len(fixes)

        # 复验:清过标记的文件 + zurg 侧读不出的文件,都要经 zurg 真读出来才算
        recheck = dict(zurg_side)
        for tid, rec in fixes.items():
            for key, f in live_files(rec):
                name = os.path.basename(f.get("path") or key)
                for rel, sz in sizes.items():
                    if os.path.basename(rel) == name and sz == f.get("bytes"):
                        recheck.setdefault(rel, (None, sz))
        good = bad = 0
        for rel, (_, size) in sorted(recheck.items()):
            status, nbytes, expected = self.dav_probe(rel, size)
            if probe_ok(status, nbytes, expected):
                good += 1
                stuck.pop(rel, None)
                self.forget("probe:" + rel)
                self.forget("zside:" + rel)
            else:
                bad += 1
                stuck[rel] = stuck.get(rel, 0) + 1
                self.log("STILL", "%s 重启后经 zurg 仍读不出(%s),第 %d 次" % (rel, status, stuck[rel]))
        # 记录是否被 zurg 启动时覆盖回去
        reverted = [tid for tid in fixes if needs_attention(self._reload(tid) or {})]
        if reverted:
            self.log("WARN", "%d 条记录重启后又被标回坏:%s" % (len(reverted), ",".join(reverted[:10])))
        self.log("VERIFY", "重启后复验 %d 个文件:%d 个读满,%d 个仍读不出" % (good + bad, good, bad))
        return 0

    @staticmethod
    def _reload(tid):
        try:
            with open(os.path.join(DATA_DIR, tid + ".json"), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="zurg 守护:RD 直连验证后清误标、重启卡住的 zurg")
    ap.add_argument("--apply", action="store_true", help="真的动手(默认只报告)")
    ap.add_argument("--probe", type=int, default=40, help="每轮经 zurg 抽查多少个文件(默认 40)")
    ap.add_argument("--no-restart", action="store_true", help="只检查不重启")
    args = ap.parse_args(argv)

    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0  # 上一轮还没跑完
    g = Guard(args)
    try:
        rc = g.run()
    except Exception as e:  # 崩了也要留一行说清楚,别静默失败
        g.log("ABORT", "守护崩了:%s: %s" % (type(e).__name__, str(e)[:300]))
        rc = 1
    finally:
        try:
            g.save_state()
        except OSError as e:
            print("状态没存上:%s" % e, file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
