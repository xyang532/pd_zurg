# -*- coding: utf-8 -*-
"""fetch_subs(assrt 源)选字幕单测,对着假 assrt + 假 Plex 跑完整的 main()。

**存在理由是《汉娜姐妹》(2026-10-31)**:assrt 上有 5 份对得上的中文,流程一份没拿到 ——
  · 最对版的 BluRay 简中是**单文件条目**(没有 filelist,文件就是条目本身),被算成 0 个文件;
  · 只看排序前 2 个条目:一个"英 简 繁"的条目里其实只有 .cht,另一个是空压缩包;
  · 条目写"英 简 繁"就把里面的 .cht 当简中(取了两者较大值)。
放宽到多看几个条目之后,又要防两种残缺字幕互相"佐证":CD1/CD2 分段、别的剪辑版。
下面的条目形状照抄 assrt 实际返回的那几个(id、文件名、有无 filelist)。
"""
import io, json, os, re, sys, tempfile
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
UPLOAD_MID = []   # 每次上传带的 mediaItemID(没带记 None)
QUERIES = []   # 本场景发出的 assrt 搜索词
HANNAH_FILE = ("/rclone/pd_zurg/movies/Hannah.and.Her.Sisters.1986.1080p."
               "Blu-ray.Remux.AVC.DTS-HD.MA.1.0-KRaLiMaRKo.mkv")


def fake_px(method, path, body=None, ctype=None, js=True, timeout=180):
    if path.startswith("/library/sections/"):
        return 200, {"Metadata": [{"ratingKey": "1", "type": "movie"}]}
    if method == "GET" and path.startswith("/library/metadata/1"):
        return 200, {"Metadata": [{
            "title": "汉娜姐妹", "originalTitle": "Hannah and Her Sisters", "year": 1986,
            "duration": int(DUR * 1000),
            "Media": W.get("media") or [{"Part": [{"file": HANNAH_FILE,
                                 "Stream": [{"streamType": 3, "language": "English", "codec": "pgs"}]}]}]}]}
    if method == "POST" and "/subtitles?" in path:
        q = urllib.parse.parse_qs(path.split("?", 1)[1])
        UPLOADS.append((q.get("title", [""])[0], body))
        UPLOAD_MID.append(q.get("mediaItemID", [None])[0])
        return 200, b""
    return 404, {}


def fake_web(url, timeout=60):
    u = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(u.query)
    if u.path.endswith("/sub/search"):
        QUERIES.append(q["q"][0])
        hits = list(W["search"])
        for year, extra in W.get("search_by_year", {}).items():
            if year in q["q"][0]:
                hits += extra
        return 200, json.dumps({"sub": {"subs": hits}}).encode()
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
    del UPLOAD_MID[:]
    del QUERIES[:]
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

print("六、对版条目只标了差一年(Plex 1986,条目 1985):靠年份 ±1 搜到;同义搜索词只发一次")
lt = run({
    "search": [],
    "search_by_year": {"1985": [{"id": 315383, "videoname": CTRL.replace(".", " ").replace("1986", "1985"),
                                 "lang": None}]},
    "detail": {315383: {"filename": CTRL + ".chs.srt", "url": "http://dl/ctrl1", "filelist": []}},
    "files": {"http://dl/ctrl1": srt(3020, 6210, SIMP)},
})
check("传了差一年条目里的简中", [n for n, _ in UPLOADS] == [CTRL + ".chs.srt"], str([n for n, _ in UPLOADS]))
check("搜了 1985 和 1987", any("1985" in q for q in QUERIES) and any("1987" in q for q in QUERIES), str(QUERIES))
norm = [re.sub(r"[\s._]+", " ", q).lower() for q in QUERIES]
check("没有重复搜索词(点号/空格版只发一次)", len(norm) == len(set(norm)) and len(QUERIES) == 4, str(QUERIES))

print("七、语言看内容:条目写「简 英」,包里的 .eng.srt 和不带语言标记的 .srt 都是英文 → 不能传")
ENG = "This is an English subtitle line"
OFF = "The.Office.S07E21.HDTV.XviD-LOL"
lt = run({
    "search": [{"id": 9, "videoname": "Hannah and Her Sisters 1986 BluRay", "lang": {"desc": "简 英"}}],
    "detail": {9: {"filename": "x.rar", "url": "http://dl/9",
                   "filelist": [{"f": "Hannah.and.Her.Sisters.1986.eng.srt", "url": "http://dl/eng"},
                                {"f": "Hannah.and.Her.Sisters.1986.BluRay.srt", "url": "http://dl/plain"}]}},
    "files": {"http://dl/eng": srt(1500, 6210, ENG), "http://dl/plain": srt(1500, 6205, ENG)},
})
check("英文没传", UPLOADS == [], str([n for n, _ in UPLOADS]))
check("NONE 写「内容不是中文×2」", "内容不是中文×2" in lt, lt.strip()[-140:])

print("八、内容定档:纯中文 ASS 记简中(文件头英文不算双语),中英双语 srt 记双语并优先")
def ass(n, last, line):
    head = "[Script Info]\nTitle: Default Aegisub file\nScriptType: v4.00+\n[V4+ Styles]\n" \
           "Style: Default,Microsoft YaHei,20,&H00FFFFFF,&H000000FF,0,0,0,0,100,100,0,0,1,2,1,2,10,10,10,1\n[Events]\n"
    def ts(x):
        return "%d:%02d:%02d.%02d" % (x // 3600, x % 3600 // 60, x % 60, int(x * 100) % 100)
    rows = []
    for i in range(n):
        a = 36 + (last - 36) * i / float(n - 1)
        rows.append("Dialogue: 0,%s,%s,Default,,0,0,0,,{\\fad(100,100)}%s" % (ts(a), ts(a + 1.5), line))
    return (head + "\n".join(rows)).encode("utf-8")
lt = run({
    "search": [{"id": 21, "videoname": CTRL.replace(".", " "), "lang": {"desc": "简"}}],
    "detail": {21: {"filename": CTRL + ".chs.ass", "url": "http://dl/ass", "filelist": []}},
    "files": {"http://dl/ass": ass(1500, 6210, SIMP)},
})
check("纯中文 ASS 传了且记简中", len(UPLOADS) == 1 and "简中" in lt and "双语" not in lt, lt.strip()[-140:])
BIL = SIMP + "\\NThis is the English line of the same cue"
lt = run({
    "search": [{"id": 22, "videoname": CTRL.replace(".", " "), "lang": {"desc": "简"}},
               {"id": 23, "videoname": CTRL.replace(".", " ") + " eng", "lang": {"desc": "简"}}],
    "detail": {22: {"filename": CTRL + ".chs.srt", "url": "http://dl/s1", "filelist": []},
               23: {"filename": CTRL + ".srt", "url": "http://dl/s2", "filelist": []}},
    "files": {"http://dl/s1": srt(1500, 6210, SIMP), "http://dl/s2": srt(1500, 6212, BIL.replace("\\N", "\n"))},
})
check("双语优先", [n for n, _ in UPLOADS] == [CTRL + ".srt"] and "双语" in lt, str([n for n, _ in UPLOADS]) + " " + lt.strip()[-80:])

print("九、多版本(2026-11-02 瑞克和莫蒂 S09 AV1):按版本判、按版本传,搜索只做一次")
PGS = {"streamType": 3, "language": "English", "codec": "pgs"}
ZH_UP = {"streamType": 3, "key": "/library/streams/77", "id": 77, "codec": "srt", "transient": "0"}


def ver(mid, codec, streams, dur=DUR, f=HANNAH_FILE):
    return {"id": mid, "videoCodec": codec, "duration": int(dur * 1000),
            "Part": [{"file": f, "Stream": streams}]}


CTRL_SCENE = {
    "search": [{"id": 315383, "videoname": CTRL.replace(".", " "), "lang": None}],
    "detail": {315383: {"filename": CTRL + ".chs.srt", "url": "http://dl/ctrl1", "filelist": []}},
    "files": {"http://dl/ctrl1": srt(3020, 6210, SIMP)},
}
lt = run(dict(CTRL_SCENE, media=[ver(501, "av1", [PGS]), ver(502, "h264", [PGS, ZH_UP])]))
check("第一版缺、第二版有(语言未知的外挂算有):只传给第一版", UPLOAD_MID == ["501"], str(UPLOAD_MID))
lt = run(dict(CTRL_SCENE, media=[ver(501, "h264", [PGS, ZH_UP]), ver(502, "av1", [PGS])]))
check("第一版有、第二版缺:传给第二版(原来合并判断会整集跳过)", UPLOAD_MID == ["502"], str(UPLOAD_MID))
check("日志标出是第几版", "[第2版 av1]" in lt, lt.strip()[-160:])
n_single = len(QUERIES)
lt = run(dict(CTRL_SCENE, media=[ver(501, "av1", [PGS]), ver(502, "h264", [PGS])]))
check("两版都缺:各传一份,各带自己的 mediaItemID", sorted(UPLOAD_MID) == ["501", "502"], str(UPLOAD_MID))
check("两版同一个文件名时搜索词不翻倍", len(QUERIES) == n_single, "%d vs %d" % (len(QUERIES), n_single))
lt = run(dict(CTRL_SCENE, media=[ver(501, "h264", [PGS, ZH_UP])]))
check("单版本已有中文:不传", UPLOADS == [], str(UPLOAD_MID))

print("十、版本片长不同:候选末条超过短版本片长,只传给长版本")
lt = run(dict(CTRL_SCENE, media=[ver(501, "h264", [PGS]), ver(502, "av1", [PGS], dur=4000.0)]))
check("只传给片长够的那一版", UPLOAD_MID == ["501"], str(UPLOAD_MID))
check("短版本记 NONE 并说明末条超片长", "超过这个版本的片长" in lt, lt.strip()[-160:])

print("十一、上传会顶掉同版本同格式的外挂:有英文 srt 挂载时不传 srt,有 ass 候选就改传 ass")
EN_MNT = {"streamType": 3, "key": "/library/streams/88", "id": 88, "codec": "srt",
          "language": "English", "languageCode": "eng", "transient": "1"}
lt = run(dict(CTRL_SCENE, media=[ver(501, "h264", [PGS, EN_MNT])]))
check("只有 srt 候选:不传", UPLOADS == [], str([n for n, _ in UPLOADS]))
check("SKIP 点名会被顶掉的那条", "SKIP" in lt and "id=88" in lt and "挂载" in lt, lt.strip()[-160:])
sc = dict(CTRL_SCENE)
sc["search"] = CTRL_SCENE["search"] + [{"id": 21, "videoname": CTRL.replace(".", " "), "lang": {"desc": "简"}}]
sc["detail"] = dict(CTRL_SCENE["detail"])
sc["detail"][21] = {"filename": CTRL + ".chs.ass", "url": "http://dl/ass", "filelist": []}
sc["files"] = dict(CTRL_SCENE["files"])
sc["files"]["http://dl/ass"] = ass(1500, 6210, SIMP)
lt = run(dict(sc, media=[ver(501, "h264", [PGS, EN_MNT])]))
check("另有 ass 候选:改传 ass,不碰英文 srt", [n for n, _ in UPLOADS] == [CTRL + ".chs.ass"],
      str([n for n, _ in UPLOADS]))
EN_ASS = dict(EN_MNT, codec="ssa", id=89, key="/library/streams/89")
lt = run(dict(CTRL_SCENE, media=[ver(501, "h264", [PGS, EN_ASS])]))
check("已有的是 ssa(不同格式):照传 srt", [n for n, _ in UPLOADS] == [CTRL + ".chs.srt"],
      str([n for n, _ in UPLOADS]))

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
