# -*- coding: utf-8 -*-
"""
fix_subs —— 自动挑一条**时间轴对得上**的中文字幕挂到 Plex 上。

为什么需要它:Plex 的 OpenSubtitles 选择是按热度排的,不是按发布版本匹配。实测《挽救计划》
被选中的是 5361 次下载的**枪版(TELESYNC)**字幕,而片源是 4K 零售版 —— 枪版开头早 61 秒、
结尾早 374 秒,偏移量在片中变化了 5 分钟,所以手动按 ms 调根本调不好:开头对准了结尾差 5 分钟。

做法两步,第二步才是关键:
  ① 按发布名打分挑候选 —— 枪版/CAM 一律拒;分辨率、片源类型对得上的优先;热度只做同分tie-break。
  ② **挂上去之后把字幕拉回来量时间码**:末条不得超过片长,且距片尾不得太远。不合格就换下一个。
     只靠名字匹配是猜,量时间码才是证据(实测:好的距片尾 18 秒,枪版差 374 秒)。

默认干跑,--apply 才改。只在发生变更时写 /log/subs.log。
"""
import argparse, io, json, os, re, sys, time
import urllib.request, urllib.parse, urllib.error

LOG_PATH = "/log/subs.log"
MAX_END_GAP = 300.0      # 末条距片尾超过这么多秒 -> 多半是别的剪辑版/枪版
MIN_CUES = 100           # 条目太少不像完整字幕

# 枪版类:片长、剪辑点、帧率都和零售版不同,时间轴必然对不上
BAD_SRC = re.compile(r"(?<![A-Za-z0-9])(TELESYNC|TS|CAM|HDCAM|HDTS|SCREENER|SCR|WORKPRINT|WP)(?![A-Za-z0-9])", re.I)
# 不同剪辑版:片长不同,除非片源本身也是该版本
CUTS = ("IMAX", "EXTENDED", "DIRECTORS", "UNCUT", "REMASTERED", "THEATRICAL")
TS_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true", help="真的改(默认干跑)")
ap.add_argument("--lang", default="zh", help="字幕语言代码")
ap.add_argument("--limit", type=int, default=5, help="单次最多处理几部")
ap.add_argument("--only", default="", help="只处理片名含该子串的")
ap.add_argument("--mode", default="bad", choices=["bad", "missing", "all"],
                help="bad=只修枪版类;missing=只补没有中文字幕的;all=两者都做")
ap.add_argument("--sections", default="1,2", help="Plex 分区号,逗号分隔")
args = ap.parse_args()

CFG = json.load(io.open("/config/settings.json", encoding="utf-8"))
BASE = CFG["Plex server address"]
TOK = os.environ.get("PLEX_TOKEN") or CFG["Plex users"][0][1]
_logged = [0]


def log(kind, msg):
    line = "%s %-8s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), kind, msg)
    with io.open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line)
    _logged[0] += 1
    print(line.rstrip())


def px(method, path, js=True, timeout=180):
    u = "%s%s%sX-Plex-Token=%s" % (BASE, path, "&" if "?" in path else "?", TOK)
    r = urllib.request.Request(u, headers={"Accept": "application/json"} if js else {})
    r.get_method = lambda: method
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            b = resp.read()
            if not js:
                return resp.status, b
            return resp.status, (json.loads(b)["MediaContainer"] if b.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:
        return 0, {}


def is_zh(s):
    t = "%s%s%s" % (s.get("language") or "", s.get("languageTag") or "", s.get("languageCode") or "")
    return ("中文" in t) or ("zh" in t.lower()) or ("chi" in t.lower())


def cues(text):
    out = []
    for h, m, s, ms in TS_RE.findall(text):
        out.append(int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0)
    return out


def check(stream_id, duration):
    """把挂上的字幕拉回来量。返回 (是否合格, 说明)。这是本工具的证据来源。"""
    st, b = px("GET", "/library/streams/%s?download=1" % stream_id, js=False)
    if st != 200:
        return False, "取不到字幕内容 HTTP %s" % st
    c = cues(b.decode("utf-8", "replace"))
    if len(c) < MIN_CUES * 2:
        return False, "条目过少(%d)" % (len(c) // 2)
    last, first = max(c), min(c)
    if last > duration + 5:
        return False, "末条 %.0fs 超出片长 %.0fs" % (last, duration)
    gap = duration - last
    if gap > MAX_END_GAP:
        return False, "末条距片尾 %.0fs(疑似别的剪辑版/枪版)" % gap
    return True, "%d 条,首 %.0fs,末条距片尾 %.0fs" % (len(c) // 2, first, gap)


STOP = {"the", "a", "an", "of", "and", "in", "on", "to", "for", "part", "le", "la"}
MIN_TITLE_OVERLAP = 0.5


def toks(s):
    out = set()
    for w in re.split(r"[^0-9A-Za-z一-鿿]+", str(s or "")):
        w = w.lower().rstrip("s")
        if len(w) >= 2 and w not in STOP and not re.fullmatch(r"\d+", w):
            out.add(w)
    return out


def same_film(sub_title, movie_titles):
    """**先确认是同一部片**,再谈时间轴。

    Plex 的字幕搜索是按片名关键词模糊匹配、不是按 IMDB 的:实测《不十分好莱坞》(imdb 正确)
    返回的 10 条全是别的电影(What Just Happened / 野孩子 / WildOceanIMAX3D),因为
    OpenSubtitles 压根没有这部片的中文字幕。而错片的时长可能刚好接近,时间码检查拦不住它。
    """
    st = toks(sub_title)
    if not st:
        return False, 0.0
    best = 0.0
    for mt in movie_titles:
        mtoks = toks(mt)
        if not mtoks:
            continue
        best = max(best, len(mtoks & st) / float(len(mtoks)))
    return best >= MIN_TITLE_OVERLAP, best


def score(title, res, file_name):
    """按发布名匹配度打分。热度只在同分时才起作用 —— 热度正是把枪版顶上来的元凶。"""
    t = title or ""
    if BAD_SRC.search(t):
        return None                     # 枪版一律拒,不给它进候选的机会
    sc = 0
    m = re.search(r"(2160|1080|720)(?=p)", t, re.I)
    if m:
        sc += 3 if int(m.group()) == res else -2
    if re.search(r"(?<![A-Za-z0-9])(BluRay|BDRemux|REMUX|WEB-?DL)(?![A-Za-z0-9])", t, re.I):
        sc += 2
    elif re.search(r"(?<![A-Za-z0-9])(WEBRip|HDRip|BRRip|DVDRip)(?![A-Za-z0-9])", t, re.I):
        sc += 1
    for cut in CUTS:
        in_sub = re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % cut, t, re.I)
        in_file = re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % cut, file_name or "", re.I)
        if bool(in_sub) != bool(in_file):
            sc -= 3                     # 剪辑版不一致 = 片长不同
    return sc


def items():
    for sec in args.sections.split(","):
        st, d = px("GET", "/library/sections/%s/all" % sec.strip())
        typ = None
        for it in (d.get("Metadata") or []):
            if it.get("type") == "show":
                typ = "4"
                break
        if typ:
            st, d = px("GET", "/library/sections/%s/all?type=4" % sec.strip())
        for it in (d.get("Metadata") or []):
            yield it["ratingKey"]


def main():
    done = 0
    for rk in items():
        if done >= args.limit:
            log("SKIP", "已达单次上限 %d,其余留待下次" % args.limit)
            break
        st, d = px("GET", "/library/metadata/%s" % rk)
        md = (d.get("Metadata") or [{}])[0]
        name = md.get("title") or ""
        if md.get("grandparentTitle"):
            name = "%s S%02dE%02d" % (md["grandparentTitle"],
                                      md.get("parentIndex") or 0, md.get("index") or 0)
        if args.only and args.only not in name:
            continue
        dur = (md.get("duration") or 0) / 1000.0
        if dur <= 0:
            continue
        subs, res, fname = [], 0, ""
        for m in md.get("Media") or []:
            res = int(re.sub(r"\D", "", str(m.get("videoResolution") or "0")) or 0) or res
            if str(m.get("videoResolution") or "").lower() == "4k":
                res = 2160
            for p in m.get("Part") or []:
                fname = os.path.basename(p.get("file") or "") or fname
                subs += [s for s in (p.get("Stream") or []) if s.get("streamType") == 3]
        zsubs = [s for s in subs if is_zh(s)]
        embedded = [s for s in zsubs if not s.get("key")]
        external = [s for s in zsubs if s.get("key")]

        if embedded:
            continue                                   # 片源自带中文,不动
        bad_now = any(BAD_SRC.search(str(s.get("title") or "")) for s in external)
        if external and not bad_now:
            continue                                   # 已有来源正常的外挂中文
        if not external and args.mode == "bad":
            continue
        if external and bad_now and args.mode == "missing":
            continue

        st, sd = px("GET", "/library/metadata/%s/subtitles?language=%s" % (rk, args.lang))
        cands = sd.get("Stream") or []
        names = [md.get("originalTitle"), md.get("title"), md.get("grandparentTitle"),
                 os.path.splitext(fname)[0]]
        scored, wrong_film = [], 0
        for s in cands:
            same, ratio = same_film(s.get("title"), [n for n in names if n])
            if not same:
                wrong_film += 1
                continue
            sc = score(s.get("title"), res, fname)
            if sc is None:
                continue
            scored.append((sc, int(s.get("score") or 0), s))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        why = "当前是枪版类字幕" if bad_now else "没有中文字幕"
        head = "%s (%sp, %s)" % (name[:40], res, why)
        if not scored:
            log("NONE", "%s -> %d 个候选无一可用(片名对不上 %d 个,其余枪版类或无结果)"
                % (head, len(cands), wrong_film))
            continue
        if not args.apply:
            log("PLAN", "%s -> 首选 %s (匹配分 %d, 下载 %s)"
                % (head, str(scored[0][2].get("title"))[:52], scored[0][0], scored[0][1]))
            done += 1
            continue

        ok = False
        for sc, dl, s in scored[:4]:
            q = urllib.parse.urlencode({"key": s["key"], "language": args.lang})
            st, _ = px("PUT", "/library/metadata/%s/subtitles?%s" % (rk, q))
            if st != 200:
                log("TRY", "%s 挂载失败 HTTP %s" % (str(s.get("title"))[:50], st))
                continue
            sid = None
            for _ in range(12):
                time.sleep(5)
                st2, d2 = px("GET", "/library/metadata/%s" % rk)
                m2 = (d2.get("Metadata") or [{}])[0]
                for mm in m2.get("Media") or []:
                    for pp in mm.get("Part") or []:
                        for ss in pp.get("Stream") or []:
                            if (ss.get("streamType") == 3 and ss.get("selected")
                                    and str(ss.get("title") or "") == str(s.get("title") or "")):
                                sid = ss["id"]
                if sid:
                    break
            if not sid:
                log("TRY", "%s 60 秒内没生效" % str(s.get("title"))[:50])
                continue
            good, detail = check(sid, dur)
            if good:
                log("FIXED", "%s -> %s;%s" % (head, str(s.get("title"))[:50], detail))
                ok = True
                break
            log("TRY", "%s 落选:%s" % (str(s.get("title"))[:50], detail))
        if not ok:
            log("NOFIX", "%s -> 试过的候选时间轴都对不上" % head)
        done += 1
    if not _logged[0]:
        print("(无需变更,未写 log)")


if __name__ == "__main__":
    main()
