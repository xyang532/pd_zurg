# -*- coding: utf-8 -*-
"""align_subs —— 把外挂中文字幕的时间轴对齐到**片源本身**。

fix_subs / fetch_subs 那套共识判据只能否掉粗差(错片、错剪辑版、枪版),因为它们互相比对的
是**字幕之间**:一堆字幕都按同一个发布版做,它们会一致地错在同一个地方。要判定"这条字幕
和我这个文件对不对得上",参照物必须来自文件自身。

参照物用**片源自带的字幕轨**(实测全库 738 部里 697 部有,94%):
  · 它和视频封装在同一个文件里,时间轴天然就是这个文件的真时间轴;
  · 取时间码**不需要解码器** —— stream copy 出来即可,连群晖那个阉割版 ffmpeg 都够用
    (它缺的是 DTS/AC3 这类音频解码器,demux 和 copy 不受影响);
  · 不用 OCR:PGS 是图形字幕,但我们只要"什么时候有字幕",PCS 段头里的 PTS 就是答案。

代价是要读文件。实测挂载盘 34.4 MB/s,一个 300 秒窗口约 35 秒(REMUX 码率下约 1.2 GB),
所以**按窗口采样,不整片扫**:取两个窗口即可 —— 一个定偏移,两个才能把"整体平移"和
"帧率漂移"区分开(前者两窗口偏移相同,后者随时间线性发散)。

对齐后怎么落地:**改写字幕文件本身再传回去**,不是设 Plex 的 offset。
  · offset(PUT /library/streams/{id}?offset=ms)确实持久化,metadata 里能读回来,但实测
    **服务端不改写下发内容** —— 偏移是客户端自己应用的,于是它的正负号约定无法从服务端
    观测到,也依赖客户端认账。改写文件则在任何客户端上都成立,而且**改完能再量一次验证**
    (偏移回到 0 = 可观测产物),这是 offset 路线给不出的证据。
  · 帧率漂移只能靠改写(offset 是常量,修不了随时间发散的误差)。
  · 改写后旧流连同它上面可能残留的手动 offset 一起删掉,避免二次补偿。

默认干跑,--apply 才改。
"""
import argparse, bisect, io, json, os, re, subprocess, sys, time
import urllib.request, urllib.parse, urllib.error

# ---- 路径:本工具跑在**宿主机**上(要用宿主机的 ffmpeg 和挂载盘),不在容器里 ----
CFG_PATH = "/volume1/docker/pd_zurg/config/settings.json"
LOG_PATH = "/volume1/docker/pd_zurg/log/subs.log"
STATE_PATH = "/volume1/docker/pd_zurg/config/align_state.json"
FFMPEG = "/usr/bin/ffmpeg"
PLEX_HOST_URL = "http://127.0.0.1:32400"   # 容器把 32400 映射到了宿主机
# Plex 容器里看到的媒体前缀 -> 宿主机上的实际路径
PLEX_PREFIX = "/rclone/pd_zurg/"
HOST_PREFIX = "/volume1/docker/pd_zurg/mnt/pd_zurg/"

TS_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})([,.])(\d{1,3})")

# —— 判定阈值,集中在这里(DRY:改一个数就是改一处)——
WIN_SEC = 300.0          # 单个采样窗口长度
MAX_SHIFT = 30.0         # 搜索偏移的范围 ±秒
BIN_SEC = 0.04           # 直方图分箱(40ms,约一帧)
MIN_PAIRS = 8            # 峰值箱里至少这么多票才认
MIN_RATIO = 0.30         # 峰值票数 / 窗口内字幕条数,低于此判为不可信
OK_SHIFT = 0.25          # |偏移| 小于此视为本来就是对的
# 判"平移"还是"漂移"看的是**整片累计**误差(|a-1| × 片长),不是两窗口的差值。
# 两窗口只差 0.17 秒听着像噪声,但那要除以窗口间距才知道斜率意味着什么;反过来,
# 一个看着很小的斜率乘上两小时片长可能就是十几秒。累计量才是观众实际感受到的东西。
# 阈值取 1 秒:实测单窗口偏移精度约 ±0.08 秒,除以 3000 秒基线再乘片长,噪声约 0.25 秒。
DRIFT_TOL = 3.0
# **只有足够可信的窗口才配参与"有没有漂移"的判断。**两个窗口的偏移差 0.5 秒,可能是真漂移,
# 也可能只是其中一个窗口量得虚(中文字幕爱把两句并一句,某些段落能配上的对儿就是少)。
# 实测:《X圣治》窗口二只有 14 票 / 39% 一致度,却把一个明摆着的 +21 秒整体平移拖成了
# "偏移随时间发散",于是该修的不修。达不到门槛的窗口只用来测偏移,不用来测斜率。
DRIFT_MIN_PAIRS = 20
DRIFT_MIN_RATIO = 0.45
# 一个可信窗口都没有时,只有偏移大到值得冒险才动手。0.3 秒的改动本来就在观感噪声里,
# 拿一个 44% 一致度的单窗口去赌它,期望收益是负的;3 秒的偏移就完全不同了。
MIN_WEAK_SHIFT = 1.0
# 常见帧率换算比(PAL/NTSC/电影),命中其一才敢按帧率漂移改写。
# **容差必须很紧**:24/23.976 只比 1.0 大 0.001,而相邻的 25/24 和 25/23.976 彼此只差
# 0.001 —— 容差一放到 0.003,任何噪声级斜率都能冒充帧率比,然后把一条只是整体平移的
# 字幕按错误比例拉伸掉(第一次真跑就撞上了:《X圣治》斜率 1.0005 被判成 24/23.976)。
FPS_RATIOS = (25.0 / 24.0, 24.0 / 25.0, 25.0 / 23.976, 23.976 / 25.0,
              24.0 / 23.976, 23.976 / 24.0)
FPS_TOL = 0.0003
# 图形字幕轨只有时间没有文本,但"什么时候有字幕"正是我们要的
PGS_CODECS = ("pgs", "hdmv_pgs_subtitle")
TEXT_CODECS = ("srt", "subrip", "ass", "ssa", "mov_text", "webvtt")
FORCED_RE = re.compile(r"forced", re.I)

_cfg = [None]
_logged = [0]


def cfg():
    """settings.json 里的 `Plex server address` 是 **http://plex:32400** —— docker 网络里的
    服务名,只有容器解析得了。本工具跑在宿主机上,必须换成宿主机能走的地址,
    否则每次查询都静默返回空,表现成"全库没有一条要处理的"(实际踩到)。"""
    if _cfg[0] is None:
        c = json.load(io.open(CFG_PATH, encoding="utf-8"))
        base = os.environ.get("PLEX_URL") or c["Plex server address"]
        host = urllib.parse.urlsplit(base).hostname or ""
        try:
            __import__("socket").gethostbyname(host)
        except Exception:
            base = PLEX_HOST_URL
        _cfg[0] = (base.rstrip("/"), os.environ.get("PLEX_TOKEN") or c["Plex users"][0][1])
    return _cfg[0]


def log(kind, msg):
    line = "%s %-8s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), kind, msg)
    try:
        with io.open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except IOError:
        pass
    _logged[0] += 1
    print(line.rstrip())


def px(method, path, body=None, ctype=None, js=True, timeout=180):
    base, tok = cfg()
    u = "%s%s%sX-Plex-Token=%s" % (base, path, "&" if "?" in path else "?", tok)
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


# ---------------------------------------------------------------- 纯逻辑(可单测)

def cue_starts(text):
    """字幕文本 -> 每条的**起始**时间(秒)。

    只取起始:结束时间在不同字幕里差异很大(有的贴着下一句,有的固定 2 秒),
    而起始时间由台词何时说出决定,才是可比的锚点。
    SRT 的毫秒和 ASS 的百分秒都能吃 —— ljust(3,'0') 把 '91' 补成 910ms。
    """
    out = []
    for i, line in enumerate(text.splitlines()):
        m = TS_RE.search(line)
        if m and ("-->" in line or line.lstrip().lower().startswith("dialogue")):
            h, mi, s, _, ms = m.groups()
            out.append(int(h) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0)
    return sorted(out)


def parse_sup(data):
    """PGS(.sup)里取"字幕出现"的时刻。

    段头 13 字节:magic 'PG' + PTS(4,90kHz) + DTS(4) + type(1) + size(2)。
    type 0x16 是 PCS(呈现构图段),其载荷第 10 字节是本次构图的对象数 —— 大于 0 是
    "显示一条字幕",等于 0 是"擦除"。只要前者。**全程不碰像素,不需要 OCR。**
    """
    out, i, n = [], 0, len(data)
    while i + 13 <= n and data[i:i + 2] == b"PG":
        pts = int.from_bytes(data[i + 2:i + 6], "big") / 90000.0
        typ = data[i + 10]
        size = int.from_bytes(data[i + 11:i + 13], "big")
        payload = data[i + 13:i + 13 + size]
        if typ == 0x16 and len(payload) > 10 and payload[10] > 0:
            out.append(pts)
        i += 13 + size
    return sorted(out)


def best_shift(sub, ref, max_shift=MAX_SHIFT, bin_s=BIN_SEC):
    """求把 sub 对到 ref 上需要的整体偏移。返回 (delta, 票数, 置信比) 或 None。

    做法是**成对时差的直方图**:对每条字幕,把附近所有参照 cue 的时差投进分箱。
    真实偏移那一箱会收到"每条字幕一票"的集中投票,而随机配对均匀摊在 ±30 秒的
    1500 个箱子里 —— 信噪比极高(实测真值箱几十票,噪声箱不到 1 票)。

    比直接互相关好在:不需要重采样成等间隔信号,也不受两边条数不等的影响
    (中文字幕常把两句英文并成一条,条数天然对不上)。
    """
    if len(sub) < MIN_PAIRS or len(ref) < MIN_PAIRS:
        return None
    ref = sorted(ref)
    hist = {}
    for s in sub:
        lo = bisect.bisect_left(ref, s - max_shift)
        hi = bisect.bisect_right(ref, s + max_shift)
        for r in ref[lo:hi]:
            k = int(round((r - s) / bin_s))
            hist[k] = hist.get(k, 0) + 1
    if not hist:
        return None
    peak = max(hist, key=lambda k: hist[k])
    # 峰值箱可能骑在两箱边界上,取邻箱一起统计,再用中位数把精度做到亚箱级
    center = peak * bin_s
    diffs = []
    for s in sub:
        lo = bisect.bisect_left(ref, s + center - 2 * bin_s)
        hi = bisect.bisect_right(ref, s + center + 2 * bin_s)
        best = None
        for r in ref[lo:hi]:
            d = r - s
            if best is None or abs(d - center) < abs(best - center):
                best = d
        if best is not None:
            diffs.append(best)
    if len(diffs) < MIN_PAIRS:
        return None
    diffs.sort()
    delta = diffs[len(diffs) // 2]
    ratio = len(diffs) / float(min(len(sub), len(ref)))
    # 票数够多但占比很低 = 这些"匹配"是随机撞上的,不是同一段对白。宁可报不出,
    # 也不能拿噪声算出来的偏移去改写字幕(改坏一条本来对的字幕比没改更糟)。
    if ratio < MIN_RATIO:
        return None
    return delta, len(diffs), ratio


def solid(r):
    """这个窗口的测量够不够硬,够硬才配参与"有没有帧率漂移"的判断。"""
    return r is not None and r[1] >= DRIFT_MIN_PAIRS and r[2] >= DRIFT_MIN_RATIO


def fit_drift(t1, d1, t2, d2):
    """两个窗口的偏移 -> 线性映射 file = a*sub + b。返回 (a, b)。"""
    if abs(t2 - t1) < 1.0:
        return 1.0, d1
    a = 1.0 + (d2 - d1) / (t2 - t1)
    b = d1 - (a - 1.0) * t1
    return a, b


def known_fps_ratio(a):
    """a 是否落在常见帧率换算比上 —— 是才敢按帧率漂移改写(否则多半是剪辑版不同)。"""
    for r in FPS_RATIOS:
        if abs(a - r) <= FPS_TOL:
            return r
    return None


def shift_text(text, a, b):
    """按 new = a*old + b 改写文本里的所有时间码,**原样保留格式**。

    不转换格式:ASS 的百分秒、SRT 的毫秒、逗号还是点,都按原样的位数写回去 ——
    改格式会连带丢掉 ASS 的定位和样式。
    """
    def rep(m):
        h, mi, s, sep, ms = m.groups()
        old = int(h) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0
        new = a * old + b
        if new < 0:
            new = 0.0
        digits = len(ms)
        # 先按输出位数四舍五入,再拆分 —— 否则 59.999 会被写成 60 秒进不了位
        scale = 10 ** digits
        total = int(round(new * scale))
        frac = total % scale
        whole = total // scale
        # 小时位宽照抄原文:SRT 写的是两位(00:00:13,914),ASS 写的是一位(0:00:13.91)。
        # 统一成 %d 会把 SRT 的小时位削掉一位,那已经不是标准 SRT 了。
        return "%s:%02d:%02d%s%s" % (str(whole // 3600).rjust(len(h), "0"),
                                     (whole // 60) % 60, whole % 60,
                                     sep, str(frac).rjust(digits, "0"))
    return TS_RE.sub(rep, text)


def pick_windows(cues, duration):
    """选采样窗口:落在字幕密集处,且互不重叠(拉得越开,帧率漂移越好测)。

    给三个而不是两个,是因为**单个窗口有真实的失败率**:参照轨在某些段落会稀到量不出
    峰值(实测《一一》片尾那 300 秒里,英文轨只有 21 条,而两条独立参照轨在片中段都
    一致给出 +2.994s)。只取两个的话,一个哑火就整部片放弃;取三个、用前两个成功的,
    哑火一个还有后备。调用方按需取,不会三个全跑。
    """
    if len(cues) < MIN_PAIRS * 2:
        return []
    out = []
    for frac in (0.15, 0.5, 0.85):
        w = max(0.0, min(cues[int(len(cues) * frac)], duration - WIN_SEC))
        if all(abs(w - x) >= WIN_SEC for x in out):
            out.append(w)
    return out


# ---------------------------------------------------------------- 与外界交互

def host_path(plex_file):
    if plex_file.startswith(PLEX_PREFIX):
        return HOST_PREFIX + plex_file[len(PLEX_PREFIX):]
    return plex_file


def ref_track(streams):
    """挑参照轨:文本优先(解析确定),其次 PGS;**排除 forced**。

    forced 轨只标外语片段,一整部片可能只有十几条,而且分布偏在特定段落 ——
    拿它当参照,窗口里往往一条都没有。它的 forced 属性 Plex 常常不给,得看标题。
    """
    out = []
    for s in streams:
        if s.get("key") or s.get("streamType") != 3:
            continue
        codec = str(s.get("codec") or "").lower()
        title = str(s.get("title") or "")
        if s.get("forced") or FORCED_RE.search(title):
            continue
        if s.get("index") is None:
            continue
        if codec in TEXT_CODECS:
            rank = 0
        elif codec in PGS_CODECS:
            rank = 1
        else:
            continue                      # vobsub 等:copy 不出可直接解析的容器,跳过
        # 英文轨台词最全;中文轨反而可能和外挂那条同源,当参照会自证清白
        lang = str(s.get("language") or "").lower()
        rank += 0 if ("english" in lang or "eng" in lang) else 1
        out.append((rank, s["index"], codec))
    out.sort()
    return out


TEXT_MUXER = {"srt": "srt", "subrip": "srt", "ass": "ass", "ssa": "ass",
              "webvtt": "webvtt", "mov_text": "srt"}


def _ffmpeg(path, index, start, dur, tail, copy=True):
    cmd = [FFMPEG, "-hide_banner", "-v", "error", "-copyts",
           "-ss", "%.3f" % start, "-t", "%.3f" % dur, "-i", path,
           "-map", "0:%d" % index]
    if copy:
        cmd += ["-c", "copy"]
    cmd += ["-f", tail, "-"]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, _ = p.communicate(timeout=900)
        return out
    except Exception:
        return b""


def extract_ref(path, index, codec, start, dur):
    """取一个窗口的参照时间码。两个坑都踩过,都在这里:

    ① **-copyts 必须加**:不加的话输出时间会被重置成从 0 开始,和字幕的绝对时间对不上
       (实测窗口起点 1800 时,加了首条是 1800.51,不加是 0.51)。
    ② **文本轨必须 -c copy**:走重编码(-f srt 不带 copy)时 ffmpeg **根本不理 -t**,
       会从窗口起点一路解到片尾 —— 实测同一个窗口,copy 用 1 秒出 84 条(正好 300 秒),
       重编码用 40 秒出 1244 条(一直到 2:53:19)。既慢又把窗口外的噪声混进来。
       PGS 走 copy 本来就正常,统一成 copy。
    """
    if codec in PGS_CODECS:
        data = _ffmpeg(path, index, start, dur, "sup")
        out = parse_sup(data)
    else:
        mux = TEXT_MUXER.get(codec, "srt")
        out = cue_starts(_ffmpeg(path, index, start, dur, mux).decode("utf-8", "replace"))
        if len(out) < MIN_PAIRS:
            # copy 不通的冷门编码(mov_text 之类)退回重编码。慢,但极少走到。
            out = cue_starts(_ffmpeg(path, index, start, dur, "srt", copy=False)
                             .decode("utf-8", "replace"))
    return [t for t in out if start - 1.0 <= t <= start + dur + 1.0]


def load_state():
    try:
        return json.load(io.open(STATE_PATH, encoding="utf-8"))
    except Exception:
        return {}


def save_state(st):
    tmp = STATE_PATH + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(st, ensure_ascii=False, indent=0, sort_keys=True))
    os.rename(tmp, STATE_PATH)


def is_zh(s):
    t = "%s%s%s" % (s.get("language") or "", s.get("languageTag") or "",
                    s.get("languageCode") or "")
    if ("中文" in t) or ("zh" in t.lower()) or ("chi" in t.lower()):
        return True
    # 我们自己上传的字幕 Plex 不打语言标记(lang=None),按 language 判会全漏掉
    return bool(s.get("key")) and not t.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的改(默认干跑)")
    ap.add_argument("--limit", type=int, default=5, help="单次最多处理几部(读盘是瓶颈)")
    ap.add_argument("--only", default="", help="只处理片名含该子串的")
    # 中文片名经 ssh 传进来会被 locale 吃掉,按 ratingKey 指定才可靠(也便于脚本化)
    ap.add_argument("--rk", default="", help="只处理这个 ratingKey(逗号分隔)")
    ap.add_argument("--sections", default="1,2")
    ap.add_argument("--recheck", action="store_true", help="连已经量过的也重量")
    args = ap.parse_args()

    state = load_state()
    done = 0
    for sec in args.sections.split(","):
        st, d = px("GET", "/library/sections/%s/all" % sec.strip())
        if any(it.get("type") == "show" for it in (d.get("Metadata") or [])):
            st, d = px("GET", "/library/sections/%s/all?type=4" % sec.strip())
        for it in (d.get("Metadata") or []):
            if done >= args.limit:
                break
            rk = it["ratingKey"]
            if args.rk and str(rk) not in args.rk.split(","):
                continue
            st, md_d = px("GET", "/library/metadata/%s" % rk)
            md = (md_d.get("Metadata") or [{}])[0]
            name = md.get("title") or ""
            if md.get("grandparentTitle"):
                name = "%s S%02dE%02d" % (md["grandparentTitle"],
                                          md.get("parentIndex") or 0, md.get("index") or 0)
            if args.only and args.only not in name:
                continue
            dur = (md.get("duration") or 0) / 1000.0
            if dur <= 0:
                continue
            for m in md.get("Media") or []:
                for p in m.get("Part") or []:
                    if done >= args.limit:
                        break
                    streams = p.get("Stream") or []
                    ext = [s for s in streams
                           if s.get("streamType") == 3 and s.get("key") and is_zh(s)]
                    if not ext:
                        continue
                    target = ext[0]
                    key = str(target["id"])
                    if not args.recheck and key in state:
                        continue
                    path = host_path(p.get("file") or "")
                    if not os.path.exists(path):
                        log("SKIP", "%s -> 文件不在:%s" % (name[:30], path[:60]))
                        state[key] = {"result": "nofile"}
                        done += 1
                        continue
                    refs = ref_track(streams)
                    if not refs:
                        log("NOREF", "%s -> 片源没有可用的参照字幕轨(只有 vobsub/forced 或全无)"
                            % name[:36])
                        state[key] = {"result": "noref"}
                        done += 1
                        continue
                    st2, b = px("GET", "/library/streams/%s?download=1" % target["id"], js=False)
                    if st2 != 200 or not b:
                        log("SKIP", "%s -> 取不到外挂字幕内容 HTTP %s" % (name[:30], st2))
                        done += 1
                        continue
                    text = b.decode("utf-8", "replace")
                    sub_cues = cue_starts(text)
                    wins = pick_windows(sub_cues, dur)
                    if not wins:
                        log("SKIP", "%s -> 字幕条数太少(%d),不足以定位" % (name[:30], len(sub_cues)))
                        state[key] = {"result": "toofew"}
                        done += 1
                        continue

                    # 一个窗口量不出来不代表这条字幕没救(参照轨在某些段落就是稀)。
                    # 逐个窗口试,**攒够两个"够可信"的就停** —— 顺利时和只取两个窗口一样贵,
                    # 遇到虚的会多跑一个窗口去换一个能用的斜率。
                    measured, used = [], None
                    for rank, idx, codec in refs[:2]:
                        measured = []
                        for w in wins:
                            rt = extract_ref(path, idx, codec, w, WIN_SEC)
                            sc = [c for c in sub_cues
                                  if w - MAX_SHIFT <= c <= w + WIN_SEC + MAX_SHIFT]
                            r = best_shift(sc, rt)
                            if r is not None:
                                measured.append((w + WIN_SEC / 2.0, r))
                                if len([m for m in measured if solid(m[1])]) >= 2:
                                    break
                        if measured:
                            used = (idx, codec)
                            break
                    if not measured:
                        log("NOREF", "%s -> 参照轨里量不出可信峰值(窗口内 cue 太少或对不上)"
                            % name[:36])
                        state[key] = {"result": "nopeak"}
                        done += 1
                        continue

                    head = "%s [%s轨#%d]" % (name[:30], used[1], used[0])
                    detail = "、".join("%.0fs处 %+.3fs(票%d/%.0f%%)%s"
                                      % (t, m[0], m[1], m[2] * 100, "" if solid(m) else "*")
                                      for t, m in measured)
                    firm = [m for m in measured if solid(m[1])]
                    if len(firm) >= 2:
                        t1, (d1, _, _) = firm[0]
                        t2, (d2, _, _) = firm[-1]
                        a, b_ = fit_drift(t1, d1, t2, d2)
                    elif firm:
                        # 只有一个够硬的窗口:就用它。**别去跟虚窗口取中位数** —— 两个值的
                        # "中位数"实际就是随便挑一个,实测《一级恐惧》因此采用了 11 票那条
                        # (-0.724)而不是 48 票那条(-1.091),改完还剩 0.367 秒没归零。
                        d1 = d2 = firm[0][1][0]
                        a, b_ = 1.0, d1
                        detail += ";只有一个可信窗口,按整体平移处理(带*的未参与)"
                    else:
                        # 一个够硬的都没有:多个窗口取中位数,让它吃掉其中量得最虚的那个
                        vals = sorted(m[1][0] for m in measured)
                        n = len(vals)
                        d1 = d2 = (vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0)
                        a, b_ = 1.0, d1
                        detail += ";没有可信窗口,取各窗口中位数"
                        if abs(d1) < MIN_WEAK_SHIFT:
                            log("UNSURE", "%s -> 没有可信窗口且偏移只有 %+.3fs,不值得冒险改;%s"
                                % (head, d1, detail))
                            state[key] = {"result": "unsure", "d": round(d1, 3)}
                            done += 1
                            continue
                    # 判据是**整片累计**漂移,不是两窗口的差值 —— 后者不除以间距没有意义
                    total_drift = abs(a - 1.0) * dur

                    if total_drift <= DRIFT_TOL:
                        a, b_ = 1.0, (d1 + d2) / 2.0
                        if abs(b_) <= OK_SHIFT:
                            log("ALIGNED", "%s -> 本来就是对的;%s" % (head, detail))
                            state[key] = {"result": "aligned", "d": round(b_, 3)}
                            done += 1
                            continue
                        fix_desc = "整体平移 %+.3fs" % b_
                    else:
                        ratio = known_fps_ratio(a)
                        if ratio is None:
                            log("MISMATCH", "%s -> 偏移随时间发散(整片累计 %.1fs,斜率 %.6f)"
                                           "且不是已知帧率比 —— 多半是别的剪辑版,不动它;%s"
                                % (head, total_drift, a, detail))
                            state[key] = {"result": "mismatch", "a": round(a, 6)}
                            done += 1
                            continue
                        fix_desc = "帧率漂移(比 %.6f,整片累计 %.1fs)" % (ratio, total_drift)

                    if not args.apply:
                        log("PLAN", "%s -> 要改:%s;%s" % (head, fix_desc, detail))
                        done += 1
                        continue

                    new_text = shift_text(text, a, b_)
                    title = str(target.get("title") or "aligned")
                    ext_name = "ass" if "Dialogue:" in text else "srt"
                    # **Plex 会把 title 当文件名去扩展名**:传 `Cure.1997.720p.BluRay.AVC-mfcorrea`
                    # 进去,挂出来叫 `Cure.1997.720p.BluRay` —— 最后一段被当成扩展名切了。
                    # 补一个真扩展名上去,切掉的正好是它,原名就完整保留了。
                    up_title = title
                    if not up_title.lower().endswith((".srt", ".ass", ".ssa")):
                        up_title = "%s.%s" % (up_title, ext_name)
                    before = set(str(s.get("id")) for s in streams
                                 if s.get("streamType") == 3 and s.get("key"))
                    q = urllib.parse.urlencode({"title": up_title, "format": ext_name})
                    st3, _ = px("POST", "/library/metadata/%s/subtitles?%s" % (rk, q),
                                body=new_text.encode("utf-8"),
                                ctype="text/plain;charset=UTF-8", js=False)
                    if st3 != 200:
                        log("FAIL", "%s -> 改写后上传失败 HTTP %s" % (head, st3))
                        done += 1
                        continue
                    # 先传新的再删旧的:中途出错也不会让片子一条字幕都不剩。
                    # **靠 id 差集认新流,不靠标题** —— 标题会被 Plex 改(见上),按标题匹配
                    # 会认不出自己刚传的那条,然后每重试一次就多堆一份重复(实测堆了 3 份)。
                    new_id = None
                    for _ in range(10):
                        time.sleep(3)
                        st4, d4 = px("GET", "/library/metadata/%s" % rk)
                        m4 = (d4.get("Metadata") or [{}])[0]
                        for mm in m4.get("Media") or []:
                            for pp in mm.get("Part") or []:
                                for ss in pp.get("Stream") or []:
                                    if (ss.get("streamType") == 3 and ss.get("key")
                                            and str(ss.get("id")) not in before):
                                        new_id = ss["id"]
                        if new_id:
                            break
                    if not new_id:
                        log("FAIL", "%s -> 上传了但找不到新流,旧流保留不动" % head)
                        done += 1
                        continue
                    px("DELETE", "/library/streams/%s" % key)
                    px("PUT", "/library/parts/%s?subtitleStreamID=%s&allParts=1"
                       % (p["id"], new_id))

                    # 可观测验证:把改完的拉回来,拿同一个参照轨再量一次
                    chk = ""
                    st5, b5 = px("GET", "/library/streams/%s?download=1" % new_id, js=False)
                    if st5 == 200 and b5:
                        c5 = cue_starts(b5.decode("utf-8", "replace"))
                        rt = extract_ref(path, used[0], used[1], wins[0], WIN_SEC)
                        sc = [c for c in c5
                              if wins[0] - MAX_SHIFT <= c <= wins[0] + WIN_SEC + MAX_SHIFT]
                        r5 = best_shift(sc, rt)
                        chk = ("改后复量 %+.3fs" % r5[0]) if r5 else "改后复量失败"
                    log("SHIFTED", "%s -> %s;%s;%s" % (head, fix_desc, detail, chk))
                    state[key] = {"result": "shifted", "d": round(b_, 3), "new": new_id}
                    # 新流也记一笔,否则下一轮会把它当没量过的重新扫一遍(白读一遍盘)
                    state[str(new_id)] = {"result": "aligned", "was": round(b_, 3)}
                    done += 1
    if args.apply or args.recheck:
        save_state(state)
    if not _logged[0]:
        print("(没有需要处理的条目)")


if __name__ == "__main__":
    main()
