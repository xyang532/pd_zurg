# -*- coding: utf-8 -*-
"""fetch_subs(assrt 源)选字幕单测,对着假 assrt + 假 Plex 跑完整的 main()。

**存在理由是《汉娜姐妹》(2026-10-31)**:assrt 上有 5 份对得上的中文,流程一份没拿到 ——
  · 最对版的 BluRay 简中是**单文件条目**(没有 filelist,文件就是条目本身),被算成 0 个文件;
  · 只看排序前 2 个条目:一个"英 简 繁"的条目里其实只有 .cht,另一个是空压缩包;
  · 条目写"英 简 繁"就把里面的 .cht 当简中(取了两者较大值)。
放宽到多看几个条目之后,又要防两种残缺字幕互相"佐证":CD1/CD2 分段、别的剪辑版。
下面的条目形状照抄 assrt 实际返回的那几个(id、文件名、有无 filelist)。
"""
import io, json, os, sys, tempfile
import importlib.util
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
for cand in (os.path.join(HERE, "..", "tools", "subs", "fetch_subs.py"),
             os.path.join(HERE, "fetch_subs.py"), "/tmp/fetch_subs.py"):
    if os.path.exists(cand):
        TARGET = os.environ.get("FETCH_SUBS_UNDER_TEST", cand)
        break
else:
    raise SystemExit("找不到 fetch_subs.py")
print("被测模块: %s" % TARGET)

TMP = tempfile.mkdtemp(prefix="fetch_subs_test_")
LOG = os.path.join(TMP, "subs.log")
SET = os.path.join(TMP, "settings.json")
KEYS = os.path.join(TMP, "keys.json")
io.open(SET, "w", encoding="utf-8").write(
    json.dumps({"Plex server address": "http://fake", "Plex users": [["u", "tok"]]}))
io.open(KEYS, "w", encoding="utf-8").write(json.dumps({"assrt_token": "t"}))
os.environ["SUBS_LOG"], os.environ["PD_SETTINGS"], os.environ["SUBS_KEYS"] = LOG, SET, KEYS
sys.argv = ["fetch_subs.py", "--apply", "--rk", "1", "--sections", "1"]
spec = importlib.util.spec_from_file_location("fetch_subs_ut", TARGET)
G = importlib.util.module_from_spec(spec)
spec.loader.exec_module(G)

fails = []


def check(name, ok, extra=""):
    print("  %-4s %s%s" % ("OK" if ok else "FAIL", name, ("  " + extra) if extra else ""))
    if not ok:
        fails.append(name)


DUR = 6415.0      # 汉娜姐妹 Remux 片长(秒)


def srt(n, last, line):
    def ts(x):
        return "%02d:%02d:%02d,%03d" % (x // 3600, x % 3600 // 60, x % 60, int(x * 1000) % 1000)
    out = []
    for i in range(n):
        a = 36 + (last - 36) * i / float(n - 1)
        out.append("%d\n%s --> %s\n%s\n" % (i + 1, ts(a), ts(a + 1.5), line))
    return "\n".join(out).encode("utf-8")


W = {}   # 当前场景:search 返回的条目、各条目详情、各下载地址的字节
UPLOADS = []


def fake_px(method, path, body=None, ctype=None, js=True, timeout=180):
    if path.startswith("/library/sections/"):
        return 200, {"Metadata": [{"ratingKey": "1", "type": "movie"}]}
    if method == "GET" and path.startswith("/library/metadata/1"):
        return 200, {"Metadata": [{
            "title": "汉娜姐妹", "originalTitle": "Hannah and Her Sisters", "year": 1986,
            "duration": int(DUR * 1000),
            "Media": [{"Part": [{"file": "/rclone/pd_zurg/movies/Hannah.and.Her.Sisters.1986.1080p."
                                         "Blu-ray.Remux.AVC.DTS-HD.MA.1.0-KRaLiMaRKo.mkv",
                                 "Stream": [{"streamType": 3, "language": "English", "codec": "pgs"}]}]}]}]}
    if method == "POST" and "/subtitles?" in path:
        q = urllib.parse.parse_qs(path.split("?", 1)[1])
        UPLOADS.append((q.get("title", [""])[0], body))
        return 200, b""
    return 404, {}


def fake_web(url, timeout=60):
    u = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(u.query)
    if u.path.endswith("/sub/search"):
        return 200, json.dumps({"sub": {"subs": W["search"]}}).encode()
    if u.path.endswith("/sub/detail"):
        return 200, json.dumps({"sub": {"subs": [W["detail"][int(q["id"][0])]]}}).encode()
    if url in W.get("codes", {}):
        return W["codes"][url], b""
    if url in W["files"]:
        return 200, W["files"][url]
    return 404, b""


REAL_WEB = G.web
G.px, G.web, G.assrt_quota = fake_px, fake_web, (lambda: 20)

SIMP, TRAD = "这是简体中文字幕", "這是繁體中文字幕"
JAP = "Hannah.And.Her.Sisters.1986.720p.BluRay.x264-Japhson"
CTRL = "Hannah.and.Her.Sisters.1986.REPACK.720p.BluRay.FLAC1.0.x264-CtrlHD"
TLF = "Hannah.And.Her.Sisters.1986.HDRip.XviD-TLF"


def run(scene):
    W.clear()
    W.update(scene)
    del UPLOADS[:]
    if os.path.exists(LOG):
        os.remove(LOG)
    G.main()
    return io.open(LOG, encoding="utf-8").read() if os.path.exists(LOG) else ""


print("一、《汉娜姐妹》原样:应当传 BluRay 简中(单文件条目),不传繁体、不传分段")
lt = run({
    "search": [
        {"id": 605590, "videoname": JAP, "lang": {"desc": "英 简 繁"}},
        {"id": 226296, "videoname": JAP, "lang": {"desc": "英 简 繁"}},
        {"id": 38499, "videoname": TLF, "lang": {"desc": "简"}},
        {"id": 315383, "videoname": CTRL.replace(".", " "), "lang": None},
        {"id": 315384, "videoname": CTRL.replace(".", " "), "lang": None},
    ],
    "detail": {
        605590: {"filename": JAP + ".rar", "url": "http://dl/605590",
                 "filelist": [{"f": JAP + ".cht.srt", "url": "http://dl/cht"}]},
        226296: {"filename": JAP + ".rar", "url": "http://dl/226296", "filelist": []},
        38499: {"filename": TLF + ".rar", "url": "http://dl/38499",
                "filelist": [{"f": TLF + ".cd1.Chs.srt", "url": "http://dl/cd1"},
                             {"f": TLF + ".cd2.Chs.srt", "url": "http://dl/cd2"}]},
        315383: {"filename": CTRL + ".chs.srt", "url": "http://dl/ctrl1", "filelist": []},
        315384: {"filename": CTRL + ".chs.srt", "url": "http://dl/ctrl2", "filelist": []},
    },
    "files": {
        "http://dl/cht": srt(1500, 6210, TRAD),
        "http://dl/cd1": srt(800, 3200, SIMP),
        "http://dl/cd2": srt(800, 3150, SIMP),
        "http://dl/ctrl1": srt(3020, 6210, SIMP),
        "http://dl/ctrl2": srt(3020, 6210, SIMP),
    },
})
names = [n for n, _ in UPLOADS]
check("只传了一条", len(UPLOADS) == 1, str(names))
check("传的是 CtrlHD 的 BluRay 简中", names and "CtrlHD.chs" in names[0], str(names))
check("没传繁体", not any(".cht" in n for n in names))
check("没传分段", not any(".cd1" in n or ".cd2" in n for n in names))
check("日志标的是简中", "UPLOAD" in lt and "简中" in lt, lt.strip().splitlines()[-1][:160] if lt else "")

print("二、只有 CD1/CD2 分段字幕:不能把半部当整部传")
lt = run({
    "search": [{"id": 38499, "videoname": TLF, "lang": {"desc": "简"}}],
    "detail": {38499: {"filename": TLF + ".rar", "url": "http://dl/38499",
                       "filelist": [{"f": TLF + ".cd1.Chs.srt", "url": "http://dl/cd1"},
                                    {"f": TLF + ".cd2.Chs.srt", "url": "http://dl/cd2"}]}},
    "files": {"http://dl/cd1": srt(800, 3200, SIMP), "http://dl/cd2": srt(800, 3150, SIMP)},
})
check("没有上传", UPLOADS == [], str([n for n, _ in UPLOADS]))
check("日志说明是分段", "分段字幕" in lt, lt.strip()[-140:])

print("三、两份同样残缺(末条在片尾前 37%)的字幕互相佐证:不能传")
A, B = "Hannah.and.Her.Sisters.1986.TVRip.chs", "Hannah.and.Her.Sisters.1986.TV.Cut.chs"
lt = run({
    "search": [{"id": 1, "videoname": A, "lang": {"desc": "简"}},
               {"id": 2, "videoname": B, "lang": {"desc": "简"}}],
    "detail": {1: {"filename": A + ".srt", "url": "http://dl/a", "filelist": []},
               2: {"filename": B + ".srt", "url": "http://dl/b", "filelist": []}},
    "files": {"http://dl/a": srt(1500, 4000, SIMP), "http://dl/b": srt(1500, 4010, SIMP)},
})
check("没有上传", UPLOADS == [], str([n for n, _ in UPLOADS]))
check("记 NOFIX", "NOFIX" in lt, lt.strip()[-140:])

print("四、下载被限流(509)记「限流」;下载主机回 402 记「拒绝下载」(那是单个文件拒下,不是限流)")
for code, want in ((509, "限流"), (402, "拒绝下载(402)")):
    lt = run({
        "search": [{"id": 315383, "videoname": CTRL.replace(".", " "), "lang": None}],
        "detail": {315383: {"filename": CTRL + ".chs.srt", "url": "https://file1.assrt.net/ctrl1",
                            "filelist": []}},
        "files": {}, "codes": {"https://file1.assrt.net/ctrl1": code},
    })
    check("下载 %d:没有上传" % code, UPLOADS == [])
    check("下载 %d:NONE 行写「%s」" % (code, want), (want + "×1") in lt and "下载失败" not in lt,
          lt.strip()[-140:])

print("五、web() 本身:509 重试、其它错误不重试(2026-10-31 起 assrt 超限回 509,原代码把它吞成 0)")
import urllib.error as UE


class FakeTime(object):
    def __init__(self):
        self.slept = []

    def sleep(self, x):
        self.slept.append(x)

    def __getattr__(self, k):
        return getattr(REAL_TIME, k)


class Resp(object):
    status = 200

    def read(self):
        return b"ok"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def scripted(codes):
    seq = list(codes)

    def urlopen(req, timeout=60):
        c = seq.pop(0)
        if c == 200:
            return Resp()
        raise UE.HTTPError("http://x", c, "x", {}, None)
    return urlopen


import time as REAL_TIME
real_open, real_paced = G.urllib.request.urlopen, G.paced
G.paced = lambda: None
API, FILE = "https://api.assrt.net/v1/sub/detail?id=1", "https://file1.assrt.net/onthefly/1/x.srt"
for label, url, codes, want, sleeps, rates in (
        ("509 两次后成功", API, [509, 509, 200], 200, 2, 2),
        ("API 主机回 402 也算超限", API, [402, 200], 200, 1, 1),
        ("下载主机回 402 = 文件拒下,不重试", FILE, [402], 402, 0, 0),
        ("下载主机回 509 照样重试", FILE, [509, 200], 200, 1, 1),
        ("一直 509:3 次后放弃并返回 509", API, [509, 509, 509], 509, 2, 3),
        ("404 不重试,如实返回 404", API, [404], 404, 0, 0)):
    G.time = FakeTime()
    G.urllib.request.urlopen = scripted(codes)
    if os.path.exists(LOG):
        os.remove(LOG)
    try:
        st, b = REAL_WEB(url)
    finally:
        G.urllib.request.urlopen = real_open
    lt = io.open(LOG, encoding="utf-8").read() if os.path.exists(LOG) else ""
    check(label, st == want and len(G.time.slept) == sleeps and lt.count("RATE") == rates,
          "st=%s sleep=%s RATE=%d" % (st, G.time.slept, lt.count("RATE")))
G.time, G.paced = REAL_TIME, real_paced

print("\n%s" % ("全部通过" if not fails else "失败 %d 项: %s" % (len(fails), "、".join(fails))))
sys.exit(1 if fails else 0)
