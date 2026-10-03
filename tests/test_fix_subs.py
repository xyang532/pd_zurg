# -*- coding: utf-8 -*-
"""fix_subs「挂候选 → 量时间码 → 留赢家」流程单测,对着一个假 Plex 跑完整的 main()。

**存在理由是一次真实的删光**:Plex 1.43.2 挂搜索结果字幕会**顶替同语言上一条**,不是追加
(2026-10-01 受控实验:挂 A → 挂 B → A 立刻消失)。旧流程逐个挂候选量完、再删落选 ——
赢家不是最后挂的那个时它早被顶掉,删完一条中文不剩,日志却写 FIXED(《完美的日子》实撞)。
所以下面的假 Plex 照这个真实行为顶替;另跑一遍"追加"语义,确认 Plex 哪天改回来也不出错。
"""
import io, json, os, sys, tempfile
import importlib.util
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
for cand in (os.path.join(HERE, "..", "tools", "subs", "fix_subs.py"),
             os.path.join(HERE, "fix_subs.py"), "/tmp/fix_subs.py"):
    if os.path.exists(cand):
        TARGET = cand
        break
else:
    raise SystemExit("找不到 fix_subs.py")
print("被测模块: %s" % TARGET)

TMP = tempfile.mkdtemp(prefix="fix_subs_test_")
LOG = os.path.join(TMP, "subs.log")
SET = os.path.join(TMP, "settings.json")
with io.open(SET, "w", encoding="utf-8") as f:
    f.write(json.dumps({"Plex server address": "http://fake", "Plex users": [["u", "tok"]]}))
os.environ["SUBS_LOG"], os.environ["PD_SETTINGS"] = LOG, SET
sys.argv = ["fix_subs.py", "--apply", "--rk", "1", "--mode", "missing", "--sections", "1"]
spec = importlib.util.spec_from_file_location("fs", TARGET)
fs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fs)
fs.time.sleep = lambda *_: None          # attach() 每轮等 4 秒,假 Plex 不用等

fails = []


def check(name, ok, extra=""):
    print("  %-4s %s%s" % ("OK" if ok else "FAIL", name, ("  " + extra) if extra else ""))
    if not ok:
        fails.append(name)


DUR = 7480.0                             # 《完美的日子》片长(秒)
FILE = "/rclone/pd_zurg/movies/Perfect.Days.2023.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-TRiToN.mkv"


def ts(t):
    ms = int(round(t * 1000))
    return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)


def srt(n, first, last):
    step = (last - first) / (n - 1)
    return "".join("%d\n%s --> %s\n台词\n\n" % (i + 1, ts(first + i * step), ts(first + i * step + 1.0))
                   for i in range(n)).encode("utf-8")


# 标题照抄 Plex 对《完美的日子》真实返回的候选
WIN = "Perfect.Days.2023.1080p.BluRay.REPACK.DDP5.1.x264-SoLaR.chs"   # 打分 5,先挂
LOSE = "Perfect Days (2023).Retail"                                  # 打分 0,后挂
JUNK = "1702851042112"                                               # 片名闸过不去
GOOD, GOOD2 = srt(452, 650, 6896), srt(1200, 5, 6899)                # 末条一致 = 共识
FEW = srt(50, 60, 6800)                                              # 条目太少,量不出


class FakePlex(object):
    """replace: "search" = 只顶替挂上来的搜索结果(实测行为);"all" = 连上传的也顶(未实测,
    用来验"找不回就报 LOST");None = 追加(旧版 Plex 的行为)。"""

    def __init__(self, cands, replace="search", existing=(), drop_after_first_search=()):
        self.cands, self.replace = list(cands), replace
        self.drop = set(drop_after_first_search)
        self.streams, self.content, self.keys = [], {}, {}
        self.nid, self.searches, self.puts = 40000, 0, 0
        for title, body, via in existing:
            self._add(title, body, via)

    def _id(self):
        self.nid += 1
        return self.nid

    def _add(self, title, body, via):
        sid = self._id()
        self.streams.append({"id": sid, "streamType": 3, "key": "/library/streams/%d" % sid,
                             "title": title, "languageCode": "zh", "_via": via})
        self.content[sid] = body

    def ext(self):
        return [s["title"] for s in self.streams if s.get("key")]

    def __call__(self, method, path, js=True, timeout=180):
        p, _, qs = path.partition("?")
        q = urllib.parse.parse_qs(qs)
        if method == "GET" and p == "/library/sections/1/all":
            return 200, {"Metadata": [{"ratingKey": "1", "type": "movie"}]}
        if method == "GET" and p == "/library/metadata/1":
            return 200, {"Metadata": [{
                "title": u"完美的日子", "originalTitle": "Perfect Days", "duration": int(DUR * 1000),
                "Media": [{"videoResolution": "1080", "Part": [{
                    "file": FILE, "Stream": [dict(s) for s in self.streams]}]}]}]}
        if method == "GET" and p == "/library/metadata/1/subtitles":
            self.searches += 1
            out = []
            for i, (title, _body) in enumerate(self.cands):
                if self.searches > 1 and title in self.drop:
                    continue
                k = "/library/streams/%d" % self._id()       # 搜索结果的 key 每次都是新的
                self.keys[k] = i
                out.append({"key": k, "title": title, "score": 100 - i})
            return 200, {"Stream": out}
        if method == "PUT" and p == "/library/metadata/1/subtitles":
            self.puts += 1
            i = self.keys.get(q.get("key", [""])[0])
            if i is None:
                return 404, {}
            if self.replace == "search":
                self.streams = [s for s in self.streams if s["_via"] != "search"]
            elif self.replace == "all":
                self.streams = [s for s in self.streams if not s.get("key")]
            self._add(self.cands[i][0], self.cands[i][1], "search")
            return 200, {}
        if p.startswith("/library/streams/"):
            sid = int(p.rsplit("/", 1)[1])
            alive = any(s["id"] == sid for s in self.streams)
            if method == "GET":                              # 搜索结果不挂上去下载不了(实测 404)
                return (200, self.content[sid]) if alive else (404, b"")
            if method == "DELETE":
                self.streams = [s for s in self.streams if s["id"] != sid]
                return (200 if alive else 404), {}
        raise AssertionError("假 Plex 没模拟这个请求: %s %s" % (method, path))


def run(plex, mode="missing"):
    io.open(LOG, "w", encoding="utf-8").close()
    fs.px, fs.args.mode = plex, mode
    fs.main()
    return io.open(LOG, encoding="utf-8").read()


print("\n[1] 顶替语义,赢家先挂(《完美的日子》原样):赢家必须还在,且记 FIXED")
pl = FakePlex([(WIN, GOOD), (LOSE, GOOD2)])
lg = run(pl)
check("片上只剩赢家", pl.ext() == [WIN], str(pl.ext()))
check("记 FIXED", "FIXED" in lg)
check("不报 LOST", "LOST" not in lg)

print("\n[2] 追加语义(旧版 Plex):落选删掉、赢家留下,不多挂")
pl = FakePlex([(WIN, GOOD), (LOSE, GOOD2)], replace=None)
lg = run(pl)
check("片上只剩赢家", pl.ext() == [WIN], str(pl.ext()))
check("记 FIXED", "FIXED" in lg)
check("只挂了两次(没有多余的重挂)", pl.puts == 2, "puts=%d" % pl.puts)

print("\n[3] 顶替语义,赢家恰好最后挂:不需要重挂")
pl = FakePlex([(WIN, srt(452, 650, 6896)), (LOSE, srt(900, 5, 5000))])
lg = run(pl)
check("赢家(末条 5000s 那个)在片上", pl.ext() == [LOSE], str(pl.ext()))
check("记 FIXED", "FIXED" in lg)
check("只挂了两次", pl.puts == 2, "puts=%d" % pl.puts)

print("\n[4] 复查模式,没选出赢家:被探测顶掉的既有字幕要找回来")
pl = FakePlex([(JUNK, GOOD2), ("Perfect.Days.2023.1080p.WEB-DL.few", FEW)],
              existing=[(JUNK, GOOD2, "search")])
lg = run(pl, mode="recheck")
check("既有字幕还在", pl.ext() == [JUNK], str(pl.ext()))
check("记 NOFIX", "NOFIX" in lg)
check("不报 LOST", "LOST" not in lg)

print("\n[5] 上传来的既有字幕被顶掉且搜不回:必须报 LOST,不能静默")
pl = FakePlex([("Perfect.Days.2023.1080p.WEB-DL.few", FEW)], replace="all",
              existing=[("Perfect.Days.aligned", GOOD, "upload")])
lg = run(pl, mode="recheck")
check("报 LOST", "LOST" in lg, lg.strip()[-160:])

print("\n[6] 赢家被顶掉后重挂失败:报 LOST,不许记 FIXED")
pl = FakePlex([(WIN, GOOD), (LOSE, GOOD2)], drop_after_first_search=[WIN])
lg = run(pl)
check("不记 FIXED", "FIXED" not in lg)
check("报 LOST", "LOST" in lg)

print("\n[7] 可证伪为错的既有字幕(末条超片长)照样摘掉,赢家留下")
pl = FakePlex([(WIN, GOOD), (LOSE, GOOD2)],
              existing=[("Perfect.Days.Wrong.Cut", srt(400, 60, DUR + 600), "search")])
lg = run(pl, mode="recheck")
check("记'摘掉既有字幕'(末条超片长那条 DROP)", u"摘掉既有字幕" in lg)
check("片上只剩赢家", pl.ext() == [WIN], str(pl.ext()))
check("记 FIXED", "FIXED" in lg)

print("\n%s" % ("全部通过" if not fails else "失败 %d 项: %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
