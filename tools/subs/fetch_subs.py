# -*- coding: utf-8 -*-
"""
fetch_subs —— 从外部中文字幕源(assrt/射手)取字幕,量过时间码再上传到 Plex。

为什么不用 Plex 自带的:它只查 OpenSubtitles,而 OpenSubtitles 的中文覆盖对老片、华语片、
冷门片很差 —— 实测全库 354 部缺中文字幕的片子里,有 56 部 Plex 一条可用候选都给不出。
assrt 有 OpenSubtitles 没有的中文压制组字幕(简/繁/双语)。

判据沿用 fix_subs 那套,但因为能**直接下载**候选,不必"挂到 Plex 再拉回来量",快一个数量级:
  ① 片名 token 重合度 —— 防错片(assrt 也会返回不相干的片子)
  ② 直接下载后量时间码,多个候选取共识 —— 末条时间的最大簇即真值
  ③ 语言偏好:双语 > 简中 > 繁中(用户 2026-08-01 定)
  ④ 赢家用 Plex 的上传接口送进去(POST .../subtitles?title=&format=,只传这两个参数)

**这套只能否掉粗差**(错片/错剪辑版/枪版/帧率漂移),**测不出亚秒级的整体偏移** ——
末条只挪 300ms 的字幕会照样过闸。真要判定那个,得拿音轨里的说话位置比对。

默认干跑,--apply 才改。
"""
import argparse, io, json, os, re, sys, time
import urllib.request, urllib.parse, urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LOG_PATH = os.environ.get("SUBS_LOG", "/log/subs.log")
KEYS = os.environ.get("SUBS_KEYS", "/config/subs_keys.json")
SETTINGS = os.environ.get("PD_SETTINGS", "/config/settings.json")
MIN_CUES = 100
TS_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")
STOP = {"the", "a", "an", "of", "and", "in", "on", "to", "for", "part", "le", "la"}
MIN_TITLE_OVERLAP = 0.5
BAD_SRC = re.compile(r"(?<![A-Za-z0-9])(TELESYNC|TS|CAM|HDCAM|HDTS|SCREENER|SCR|WORKPRINT)(?![A-Za-z0-9])", re.I)
SUB_EXT = (".srt", ".ass", ".ssa")
# 分段字幕(给 CD1/CD2 两段片源做的)每份只有半部片。只认 cd/disc 加数字 —— "Part 1"
# 是片名的一部分(《死亡圣器 上》),不能当分段。
SPLIT_RE = re.compile(r"(?<![A-Za-z0-9])(cd|disc)[\s._-]?\d(?![0-9])", re.I)

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
ap.add_argument("--only", default="")
# 事件触发时只处理刚入库的那一个条目 —— 中文片名经 ssh/locale 传不可靠,按 ratingKey 指
ap.add_argument("--rk", default="", help="只处理这些 ratingKey(逗号分隔)")
ap.add_argument("--limit", type=int, default=5)
# 原来默认 2、注释写"下载是稀缺资源别调大" —— 那是把 /v1/user/quota 误读成额度时定的;
# 实际是按分钟的速率限制(见 PACE),web() 已按它节流。只看 2 个条目时,对的那个排第 3
# 以后就永远轮不到(《汉娜姐妹》:前 2 名一个只有繁体、一个 0 文件,简中排第 3–5)。
ap.add_argument("--probe", type=int, default=6,
                help="每部片最多看几个 assrt 条目(每个条目 1 次详情 + 至多 2 次下载)")
ap.add_argument("--sections", default="1,2")
args = ap.parse_args()

CFG = json.load(io.open(SETTINGS, encoding="utf-8"))
BASE = CFG["Plex server address"]
TOK = os.environ.get("PLEX_TOKEN") or CFG["Plex users"][0][1]
K = json.load(io.open(KEYS, encoding="utf-8"))
ASSRT = K["assrt_token"]
_logged = [0]


def log(kind, msg):
    line = "%s %-8s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), kind, msg)
    with io.open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line)
    _logged[0] += 1
    print(line.rstrip())


def px(method, path, body=None, ctype=None, js=True, timeout=180):
    u = "%s%s%sX-Plex-Token=%s" % (BASE, path, "&" if "?" in path else "?", TOK)
    h = {"Accept": "application/json"} if js else {"Accept": "text/plain, */*; q=0.01"}
    if ctype:
        h["Content-Type"] = ctype
    r = urllib.request.Request(u, data=body, headers=h)
    r.get_method = lambda: method
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            b = resp.read()
            return resp.status, (json.loads(b)["MediaContainer"] if js and b.strip() else b)
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:
        return 0, {}


def web(url, timeout=60):
    """返回 (HTTP 状态, 字节)。assrt 超限(rate_limited())时等 RATE_WAIT 秒重试,共 RATE_TRIES 次,
    每次记一行 RATE;其它 HTTP 错误如实返回状态码,连不上返回 0。
    原来 `except Exception` 把 HTTPError 也吞成 0,下载处那段"遇 402 等 30 秒"从来走不到,
    限流全被记成"下载失败"/"无匹配文件"(2026-10-31 重跑 46 条,前 19 条全是这样)。"""
    code = 0
    for i in range(RATE_TRIES):
        paced()
        try:
            r = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            code = e.code
            if not rate_limited(code, url):
                return code, b""
            log("RATE", "assrt 超限(HTTP %d),第 %d/%d 次%s" % (
                code, i + 1, RATE_TRIES, ",等 %d 秒再试" % RATE_WAIT if i + 1 < RATE_TRIES else ",放弃这次请求"))
            if i + 1 < RATE_TRIES:
                time.sleep(RATE_WAIT)
        except Exception:
            return 0, b""
    return code, b""


def toks(s):
    out = set()
    for w in re.split(r"[^0-9A-Za-z一-鿿]+", str(s or "")):
        w = w.lower().rstrip("s")
        if len(w) >= 2 and w not in STOP and not re.fullmatch(r"\d+", w):
            out.add(w)
    return out


NOISE = {"bluray", "blu", "ray", "webrip", "web", "dl", "hdrip", "brrip", "dvdrip", "remux",
         "x264", "x265", "h264", "h265", "hevc", "avc", "aac", "ac3", "dts", "hd", "ma",
         "1080p", "720p", "2160p", "480p", "1080i", "chs", "cht", "eng", "chi", "zh", "cn",
         "srt", "ass", "ssa", "sub", "简", "繁", "双语", "中英", "字幕", "国配", "特效",
         "us", "uk", "proper", "repack", "extended", "criterion", "internal", "limited"}


CUT = re.compile(r"[.\s_\-\[(]((19|20)\d{2}|S\d{1,2}(E\d{1,3})?)([.\s_\-\])]|$)", re.I)


def clean_title(fname):
    """从发布名里切出真正的片名 —— 年份或 SxxExx 之前的部分。

    直接拿整个文件名当片名会**反过来咬自己**:`Cure.1997.Criterion.1080p...FLAC-SARTRE`
    里的 criterion/flac/sartre 都会被算成片名 token,把重合率稀释到 0.33,于是正确的
    `Cure.1997.720p.BluRay` 反被拦下。而这类片子(日文原名+中文译名)唯一带英文名的
    恰恰只有文件名,切干净它是必需的。
    """
    base = os.path.splitext(str(fname or ""))[0]
    m = CUT.search(base)
    return base[:m.start()] if m else base


def same_film(sub_title, movie_titles):
    """**双向覆盖率取大**,门槛 0.7。

    单看"片名 token 被覆盖了多少"会被同系列电影骗过:《火焰杯》的 token 是
    {harry,potter,goblet,fire},而《密室》的候选贡献 {harry,potter} 正好 50% —— 压线通过,
    而区分度恰恰全在漏掉的那两个词上(实测踩到)。反过来只看候选覆盖率,又会被
    长片名的缩写发布骗过。取两者的大者,再剥掉发布噪声词,两类都挡得住。
    """
    st = toks(sub_title) - NOISE
    if not st:
        return False
    for mt in movie_titles:
        mt2 = toks(mt) - NOISE
        if not mt2:
            continue
        inter = len(mt2 & st)
        if max(inter / float(len(mt2)), inter / float(len(st))) >= 0.7:
            return True
    return False


def decode(raw):
    """中文字幕编码很杂:实测碰到 utf-16、gb18030 也很常见,不能只试 utf-8。"""
    for enc in ("utf-8-sig", "utf-8", "utf-16", "gb18030", "big5"):
        try:
            t = raw.decode(enc)
            if t.count("�") < 20:
                return t, enc
        except Exception:
            continue
    return None, None


def cue_times(text):
    return sorted(int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0
                  for h, m, s, ms in TS_RE.findall(text))


def lang_rank(desc, filename):
    """语言偏好:双语 > 简中 > 繁中(用户定)。desc 来自 assrt 的 lang.desc。"""
    t = "%s %s" % (desc or "", filename or "")
    if re.search(r"双语|中英|c&e|chs&eng|zh&e|\.zh_?en|eng&chs", t, re.I):
        return 3
    if re.search(r"简|chs|gb|sc(?![a-z])|zh-?cn", t, re.I):
        return 2
    if re.search(r"繁|cht|big5|tc(?![a-z])|zh-?tw", t, re.I):
        return 1
    return 0


def assrt_search(query):
    u = "https://api.assrt.net/v1/sub/search?%s" % urllib.parse.urlencode(
        {"token": ASSRT, "q": query, "cnt": 15})
    st, b = web(u)
    if st != 200:
        return []
    try:
        return ((json.loads(b).get("sub") or {}).get("subs")) or []
    except Exception:
        return []


_last = [0.0]
# assrt 限速(不是每日额度)。文档写 20 次/分钟,2026-10-31 实测像令牌桶:/v1/user/quota 空闲时最多回 5,
# 每 3.2 秒 1 次连发 15 次有 4 次被拒 ⇒ 可持续约 7.5 次/分钟。超了回 **509**
# `{"status":30900,"errmsg":"you are exceeding request limits"}`(早先 API 超限回 402)。
PACE = 12.5               # 秒/次,4.8 次/分钟,给监听器与手动重跑同时跑留余量
RATE_WAIT, RATE_TRIES = 60, 3


def rate_limited(code, url):
    """509 一律是超限;402 只在 API 主机上算超限。**下载主机 file*.assrt.net 回 402 是这个文件
    拒绝下载**,和速率无关:《第一头牛》EVO 条目的 .ass 从 05:48 到 06:11 一直 402,
    同一时段别的文件照常 200 —— 重试只是白等。"""
    return code == 509 or (code == 402 and urllib.parse.urlsplit(url).netloc == "api.assrt.net")


def paced():
    """按 PACE 节流。只管本进程;监听器和手动重跑同时跑时靠 web() 的超限重试兜住。"""
    d = PACE - (time.time() - _last[0])
    if d > 0:
        time.sleep(d)
    _last[0] = time.time()


def assrt_quota():
    """返回当前这一分钟的剩余请求数(20 次/分钟的配额)。"""
    st, b = web("https://api.assrt.net/v1/user/quota?%s"
                % urllib.parse.urlencode({"token": ASSRT}))
    try:
        return int(((json.loads(b).get("user") or {}).get("quota")))
    except Exception:
        return -1


def assrt_files(sub_id):
    u = "https://api.assrt.net/v1/sub/detail?%s" % urllib.parse.urlencode(
        {"token": ASSRT, "id": sub_id})
    st, b = web(u)
    if st != 200:
        return []
    try:
        s = ((json.loads(b).get("sub") or {}).get("subs") or [{}])[0]
        files = [(f.get("f"), f.get("url")) for f in (s.get("filelist") or [])
                 if f.get("url") and str(f.get("f", "")).lower().endswith(SUB_EXT)]
        # 只有压缩包条目才有 filelist;单个字幕文件直接上传的条目,文件就是条目本身
        # (filename + url)。原来只看 filelist,这类条目一律算 0 个文件 ——
        # 《汉娜姐妹》唯一对版的 BluRay 简中(CtrlHD.chs.srt)就是这么漏的。
        if not files and s.get("url") and str(s.get("filename") or "").lower().endswith(SUB_EXT):
            files = [(s["filename"], s["url"])]
        return files
    except Exception:
        return []


def consensus(lasts, tol=60.0):
    best, bestn = None, 0
    for x in lasts:
        n = sum(1 for y in lasts if abs(y - x) <= tol)
        if n > bestn:
            best, bestn = x, n
    return best, bestn


def is_zh(s):
    t = "%s%s%s" % (s.get("language") or "", s.get("languageTag") or "", s.get("languageCode") or "")
    return ("中文" in t) or ("zh" in t.lower()) or ("chi" in t.lower())


def items():
    for sec in args.sections.split(","):
        st, d = px("GET", "/library/sections/%s/all" % sec.strip())
        if any(it.get("type") == "show" for it in (d.get("Metadata") or [])):
            st, d = px("GET", "/library/sections/%s/all?type=4" % sec.strip())
        for it in (d.get("Metadata") or []):
            yield it["ratingKey"]


def main():
    done = 0
    print("assrt 本分钟剩余请求数: %s(已按 %.1f 秒/次节流)" % (assrt_quota(), PACE))
    keep = set(x for x in args.rk.split(",") if x)
    for rk in items():
        if keep and str(rk) not in keep:
            continue
        if done >= args.limit:
            log("SKIP", "已达单次上限 %d" % args.limit)
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
        subs, fname = [], ""
        for m in md.get("Media") or []:
            for p in m.get("Part") or []:
                fname = os.path.basename(p.get("file") or "") or fname
                subs += [s for s in (p.get("Stream") or []) if s.get("streamType") == 3]
        # **上传上去的字幕 lang 是 None**(Plex 不给上传件打语言标记),所以只看 is_zh 会
        # 认不出自己传的,每跑一轮就重传一份(实测把《X圣治》传成了 3 条)。
        # 外挂且语言未知的一律当"已有",宁可漏补也不制造重复。
        if [s for s in subs
                if is_zh(s) or (s.get("key") and not (s.get("language") or "").strip())]:
            continue

        titles = [t for t in (md.get("originalTitle"), md.get("title"),
                              md.get("grandparentTitle"), clean_title(fname)) if t]
        # 剧集的 title 是**单集名**(如"跨维度电视2"),拿它去查字幕站必然空手 ——
        # 中文字幕站按"剧名 + 季/集"索引。所以剧集改用 grandparentTitle + SxxExx。
        ep = None
        if md.get("grandparentTitle"):
            ep = (int(md.get("parentIndex") or 0), int(md.get("index") or 0))
            show = md["grandparentTitle"]
            queries = ["%s S%02dE%02d" % (show, ep[0], ep[1]),
                       "%s S%02d" % (show, ep[0]), show]
            titles = [t for t in (show, md.get("originalTitle"), clean_title(fname)) if t]
        else:
            queries = ["%s %s" % (t, md.get("year") or "") for t in
                       (clean_title(fname), md.get("originalTitle"), md.get("title")) if t]
        seen_ids, cands = set(), []
        for q in queries:
            for s in assrt_search(q.strip()):
                sid = s.get("id")
                vn = str(s.get("videoname") or s.get("native_name") or "")
                # 条目名常常是垃圾(实测有个条目就叫 "BluRay",里面却装着 79 个正确命名的文件),
                # 所以条目名**不作否决依据** —— 真正的身份在文件名上,逐个文件再验。
                if sid in seen_ids or BAD_SRC.search(vn):
                    continue
                seen_ids.add(sid)
                cands.append((lang_rank((s.get("lang") or {}).get("desc"), vn), vn, sid))
        cands.sort(key=lambda x: -x[0])
        head = "%s (%.0f 分)" % (name[:36], dur / 60)
        if not cands:
            log("NONE", "%s -> assrt 无匹配片名的候选" % head)
            done += 1
            continue

        probed, why_fail = [], {}
        for rank, vn, sid in cands[:args.probe]:

            # **先筛后截断**。原来先取前 2 个文件再筛集号,而 13 集的包里 S03E11 是第 11 个,
            # 永远轮不到 —— 76 个 NONE 里很大一部分是这么来的。
            files = []
            for fn, url in assrt_files(sid):
                if ep and not re.search(r"(?<![0-9])S0*%dE0*%d(?![0-9])" % ep, fn, re.I):
                    continue
                # 分段字幕的两段末条时间彼此接近,会互相"佐证"成共识,把半部字幕当整部传上去
                if SPLIT_RE.search(fn):
                    why_fail["分段字幕"] = why_fail.get("分段字幕", 0) + 1
                    continue
                # 合集条目(如"哈利波特1-8部合集")里装着多部电影的字幕,逐个文件验片名
                if not same_film(fn, titles):
                    continue
                files.append((fn, url))
            for fn, url in files[:2]:
                st2, raw = web(url)
                if st2 != 200 or not raw:
                    k = ("限流" if rate_limited(st2, url) else
                         "拒绝下载(402)" if st2 == 402 else "下载失败")
                    why_fail[k] = why_fail.get(k, 0) + 1
                    continue
                txt, enc = decode(raw)
                if not txt:
                    why_fail["解码失败"] = why_fail.get("解码失败", 0) + 1
                    continue
                c = cue_times(txt)
                if len(c) < MIN_CUES:
                    why_fail["条目过少"] = why_fail.get("条目过少", 0) + 1
                    continue
                if c[-1] > dur + 5:
                    why_fail["末条超片长"] = why_fail.get("末条超片长", 0) + 1
                    continue
                # 文件名上的语言标记比条目的 lang.desc 可靠:条目常写"英 简 繁",里面却只装
                # 一个 .cht.srt(《汉娜姐妹》605590)。原来取两者较大值,繁体文件被当成简中。
                probed.append({"rank": lang_rank("", fn) or rank, "name": fn, "vn": vn,
                               "n": len(c), "first": c[0], "last": c[-1],
                               "text": txt, "enc": enc})
        if not probed:
            log("NONE", "%s -> %d 个条目里没有可用文件(%s)"
                % (head, len(cands),
                   "、".join("%s×%d" % kv for kv in sorted(why_fail.items())) or "无匹配文件"))
            done += 1
            continue

        truth, votes = consensus([p["last"] for p in probed])
        agree = [p for p in probed if abs(p["last"] - truth) <= 60.0]
        if not agree:
            log("NOFIX", "%s -> %d 个候选彼此不一致" % (head, len(probed)))
            done += 1
            continue
        # 只有一个候选时没有佐证可依,退回一条宽松的绝对闸:末条不得早于片长的 75%。
        # 实测《办公室 S05E21》:片长 33 分(加长版),而唯一候选是 21 分的电视播出版字幕,
        # 差了 36% —— 共识机制在样本量为 1 时形同虚设,必须有这道兜底。
        # 现在每部看 6 个条目,两份同样残缺的字幕(别的剪辑版、分段)互相佐证的机会变大,
        # 所以这道闸不论票数都要过:整部片的字幕末条不可能离片尾超过四分之一。
        if (dur - truth) > dur * 0.25:
            log("NOFIX", "%s -> %d 个候选的末条都在片尾前 %.0fs(%.0f%%),像分段或别的剪辑版,不采用"
                % (head, votes, dur - truth, 100.0 * (dur - truth) / dur))
            done += 1
            continue
        w = max(agree, key=lambda p: (p["rank"], p["n"]))
        lang_desc = {3: "双语", 2: "简中", 1: "繁中", 0: "未知"}[w["rank"]]
        detail = ("%s;%s %d 条,首 %.0fs,末 %.0fs(%d/%d 一致,距片尾 %.0fs,编码 %s)"
                  % (w["name"][:44], lang_desc, w["n"], w["first"], w["last"],
                     votes, len(probed), dur - w["last"], w["enc"]))
        if not args.apply:
            log("PLAN", "%s -> %s" % (head, detail))
            done += 1
            continue

        ext = os.path.splitext(w["name"])[1].lstrip(".").lower() or "srt"
        q = urllib.parse.urlencode({"title": w["name"], "format": ext})
        st3, _ = px("POST", "/library/metadata/%s/subtitles?%s" % (rk, q),
                    body=w["text"].encode("utf-8"),
                    ctype="text/plain;charset=UTF-8", js=False)
        if st3 != 200:
            log("FAIL", "%s -> 上传失败 HTTP %s" % (head, st3))
            done += 1
            continue
        log("UPLOAD", "%s -> %s" % (head, detail))
        done += 1
    if not _logged[0]:
        print("(无需变更，未写 log)")


if __name__ == "__main__":
    main()
