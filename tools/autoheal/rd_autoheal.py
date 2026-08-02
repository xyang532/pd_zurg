# -*- coding: utf-8 -*-
"""
rd_autoheal —— RD 侧片源挂掉后自动换成可用的新片源。

跑在 pd_zurg 容器里(要用 plex_debrid 的抓取器和版本档)。默认干跑,--apply 才动手。

判据(2026-08-01 定案):
  · 全自动,不需人工确认
  · 分辨率不低于原件
  · 替换文件体积 >= 原件的 80%
  · 只写独立 log(/log/autoheal.log),且**仅在状态变化时**写
  · 铁律:先加新的、实测能 unrestrict,才动旧的;任一步失败就撤销新的、保留旧的

为什么这么设计:
  · **死档判定只认 451**。RD 对 DMCA 下架的文件永久返回 451 infringing_file;其它非 200
    (404/503/超时)大多是瞬时的,zurg 自己能修 —— 所以要连续 CONFIRM_RUNS 次看到才算数,
    否则一次网络抖动就会触发替换。
  · **一个种子里可能只死一部分**(实测:黑镜 S01 包里只有 E02 死了,E01/E03 都活着)。
    所以只有**整个种子全死**才删旧种;部分死亡就只补新片源、保留旧种,否则会连累活着的集。
  · 选片标准**直接复用 settings.json 里的版本档**(releases.sort),不另写一套排序 ——
    你改版本档,这里自动跟着变。只把两条规则摘掉:cache status(缓存状态由"加进去看秒不秒"
    实测,比抓取器的标记准)和 bitrate(要 Plex 时长,这里拿不到,留着会全归零)。
"""
import argparse, copy, io, json, os, re, ssl, sys, time
import urllib.request, urllib.parse, urllib.error

os.chdir("/")
sys.path.insert(0, "/plex_debrid")

STATE_PATH = "/config/autoheal_state.json"
LOG_PATH = "/log/autoheal.log"
CONFIRM_RUNS = 2          # 非 451 的异常要连续几次才当真
MIN_SIZE_RATIO = 0.80     # 替换件体积不得低于原件的这个比例
MAX_429_RETRY = 4         # 被限流就退避重试,重试完还是 429 就当"没测到"
CTX = ssl.create_default_context()

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true", help="真的执行替换(默认干跑)")
ap.add_argument("--rate", type=float, default=1.0, help="RD 调用速率上限,次/秒")
ap.add_argument("--recheck-days", type=float, default=0.0,
                help="健康链接多少天内不重测。默认 0 = 每轮全扫(检测延迟 <=1 天)。"
                     "设成 7 能把日常调用量砍到 1/7,代价是新死的档最长 7 天才发现")
ap.add_argument("--limit", type=int, default=3, help="单次最多替换几个,防跑飞")
ap.add_argument("--tries", type=int, default=8, help="每个死档最多试几个候选")
ap.add_argument("--only", default="", help="只处理文件名含该子串的死档(调试用)")
ap.add_argument("--scan-only", action="store_true", help="只扫描不找替代")
ap.add_argument("--sample", type=int, default=0, help="只扫前 N 个种子(自测用)")
args = ap.parse_args()

_last_call = [0.0]


def paced():
    """RD 有限流,失败路径冲得越猛越失败(本项目栽过)。全局节流。"""
    gap = 1.0 / max(0.1, args.rate)
    d = gap - (time.time() - _last_call[0])
    if d > 0:
        time.sleep(d)
    _last_call[0] = time.time()


# ---------------------------------------------------------------- RD API
import ui                                    # noqa: E402
ui.config_dir = "/config"
ui.load()
CFG = json.load(io.open("/config/settings.json", encoding="utf-8"))
RD_KEY = CFG["Real Debrid API Key"]


def rd(path, data=None, method=None, timeout=60):
    paced()
    r = urllib.request.Request(
        "https://api.real-debrid.com/rest/1.0" + path,
        headers={"Authorization": "Bearer " + RD_KEY, "User-Agent": "Mozilla/5.0"})
    if data is not None:
        r.data = urllib.parse.urlencode(data).encode()
    if method:
        r.get_method = lambda: method
    try:
        with urllib.request.urlopen(r, timeout=timeout, context=CTX) as resp:
            b = resp.read()
            return resp.status, (json.loads(b) if b.strip() else {})
    except urllib.error.HTTPError as e:
        b = e.read()
        try:
            return e.code, json.loads(b)
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": type(e).__name__ + ": " + str(e)[:60]}


def rd_torrents():
    out = []
    for pg in range(1, 30):
        st, b = rd("/torrents?limit=100&page=%d" % pg)
        if st != 200 or not b:
            break
        out += b
    return out


# ---------------------------------------------------------------- Plex 索引
def plex_index():
    """basename -> {imdb(剧集级/电影), res, size, show, s, e, title}"""
    base = CFG["Plex server address"]
    tok = os.environ.get("PLEX_TOKEN") or CFG["Plex users"][0][1]

    def px(p):
        u = "%s%s%sX-Plex-Token=%s" % (base, p, "&" if "?" in p else "?", tok)
        r = urllib.request.Request(u, headers={"Accept": "application/json"})
        return json.loads(urllib.request.urlopen(r, timeout=90).read())["MediaContainer"]

    def imdb_of(md):
        for g in (md.get("Guid") or []):
            if str(g.get("id", "")).startswith("imdb://"):
                return g["id"][7:]
        return None

    idx, show_imdb = {}, {}
    secs = px("/library/sections")["Directory"]
    for s in secs:
        if s["type"] == "movie":
            for it in (px("/library/sections/%s/all?includeGuids=1" % s["key"]).get("Metadata") or []):
                m = (it.get("Media") or [{}])[0]
                for p in (m.get("Part") or []):
                    idx[os.path.basename(p.get("file") or "")] = {
                        "imdb": imdb_of(it), "res": _res(m), "size": p.get("size") or 0,
                        "kind": "movie", "title": it.get("title"), "s": 0, "e": 0}
        else:
            for ep in (px("/library/sections/%s/all?type=4&includeGuids=1" % s["key"]).get("Metadata") or []):
                grk = str(ep.get("grandparentRatingKey") or "")
                if grk and grk not in show_imdb:
                    try:
                        sh = (px("/library/metadata/%s?includeGuids=1" % grk).get("Metadata") or [{}])[0]
                        show_imdb[grk] = imdb_of(sh)
                    except Exception:
                        show_imdb[grk] = None
                m = (ep.get("Media") or [{}])[0]
                for p in (m.get("Part") or []):
                    idx[os.path.basename(p.get("file") or "")] = {
                        "imdb": show_imdb.get(grk), "res": _res(m), "size": p.get("size") or 0,
                        "kind": "episode", "title": ep.get("grandparentTitle"),
                        "s": int(ep.get("parentIndex") or 0), "e": int(ep.get("index") or 0)}
    return idx


def _res(media):
    v = str(media.get("videoResolution") or "")
    if v.lower() in ("4k", "2160"):
        return 2160
    try:
        return int(re.sub(r"\D", "", v) or 0)
    except Exception:
        return 0


# ---------------------------------------------------------------- 状态 & 日志
def load_state():
    try:
        return json.load(io.open(STATE_PATH, encoding="utf-8"))
    except Exception:
        return {"links": {}, "last_run": None}


def save_state(st):
    st["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    io.open(STATE_PATH, "w", encoding="utf-8").write(json.dumps(st, indent=1, ensure_ascii=False))


_logged = [0]


def log(kind, msg):
    """只在状态变化 / 发生动作时调用 —— 平安无事的一轮不写任何东西。"""
    line = "%s %-7s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), kind, msg)
    with io.open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line)
    _logged[0] += 1
    print(line.rstrip())


# ---------------------------------------------------------------- 健康扫描
def unrestrict_code(link):
    """返回 (HTTP 码, error 串)。429 会退避重试 —— 限流不是健康信号,不能当证据。"""
    delay = 2.0
    for attempt in range(MAX_429_RETRY + 1):
        st, b = rd("/unrestrict/link", {"link": link})
        err = b.get("error", "") if isinstance(b, dict) else ""
        if st != 429:
            return st, err
        if attempt < MAX_429_RETRY:
            time.sleep(delay)
            delay *= 2
    return 429, "rate_limited"


def scan(tors, state):
    """返回 [(torrent, dead_index_list, live_count)];顺带把状态变化写进 log。"""
    links_state = state.setdefault("links", {})
    found = []
    now = time.time()
    skipped = probed = 0
    unknown = {}
    for t in tors:
        links = t.get("links") or []
        info = None
        dead = []
        for i, ln in enumerate(links):
            key = "%s#%d" % (t["id"], i)
            prev = links_state.get(key, {})
            # 增量复查:健康且近期测过的跳过。全库每轮重测一遍既慢又必然撞限流。
            if (prev.get("health") == "ok"
                    and now - float(prev.get("ts") or 0) < args.recheck_days * 86400):
                skipped += 1
                continue
            code, err = unrestrict_code(ln)
            probed += 1
            if code == 451:
                health, strikes = "dead", CONFIRM_RUNS
            elif code == 200:
                health, strikes = "ok", 0
            elif code == 429 or code == 0 or code >= 500:
                # 限流 / 网络错 / 服务端故障 = **没测到**,不是"文件坏了"。
                # 这里绝不能记 strike —— 否则连续两轮异常就会把健康文件判死并触发替换。
                # 但三者必须**分档记**:混成一档会把"hoster 挂了"读成"被限流",调速率是白调。
                kind = "限流429" if code == 429 else ("网络错" if code == 0 else "hoster不可用%d" % code)
                unknown[kind] = unknown.get(kind, 0) + 1
                links_state[key] = dict(prev, last_unknown=int(now), code=code, why=kind)
                continue
            else:
                # 404 之类:文件确实取不到,但可能是 zurg 尚未修复 —— 要连续看到才算数
                strikes = int(prev.get("strikes", 0)) + 1
                health = "dead" if strikes >= CONFIRM_RUNS else "suspect"
            # 首次见到且健康 —— 那是建基线,不是状态变化,不写 log(否则首轮会刷几百行)
            first_and_fine = (not prev) and health == "ok"
            if prev.get("health") != health and not first_and_fine:
                if info is None:
                    _, info = rd("/torrents/info/" + t["id"])
                fn = _fname(info, i)
                log("CHANGE", "%s -> %s  HTTP %s %s  | %s"
                    % (fn[:70], health, code, err, t.get("filename", "")[:50]))
            links_state[key] = {"health": health, "strikes": strikes, "code": code, "ts": now}
            if health == "dead":
                dead.append(i)
            # 全扫要十几分钟,中途被打断(容器重启)不该把整轮进度丢掉
            if probed % 50 == 0:
                save_state(state)
        # 本轮被限流跳过、但历史上已判死的链接也要处理 —— 否则一次 429 就会让死档漏掉一轮
        for i in range(len(links)):
            if i not in dead and links_state.get("%s#%d" % (t["id"], i), {}).get("health") == "dead":
                dead.append(i)
        if dead:
            if info is None:
                _, info = rd("/torrents/info/" + t["id"])
            found.append((t, info, sorted(set(dead)), len(links) - len(set(dead))))
    tot_unknown = sum(unknown.values())
    print("  探测 %d 条,跳过(近期健康)%d 条,没测到 %d 条 %s"
          % (probed, skipped, tot_unknown, unknown or ""))
    if tot_unknown:
        log("UNKNOWN", "%d 条链接未能判定(%s);不计入健康判据,留待下轮"
            % (tot_unknown, ", ".join("%s×%d" % kv for kv in sorted(unknown.items()))))
    return found


def _fname(info, i):
    files = [f for f in (info.get("files") or []) if f.get("selected")]
    if i < len(files):
        return os.path.basename(files[i].get("path", "") or "")
    return "?"


# ---------------------------------------------------------------- 找替代
def build_version():
    """复用用户的版本档,摘掉这里用不了的两条规则(理由见文件头)。"""
    import releases
    raw = None
    for v in releases.sort.versions:
        if "̶" not in v[0]:          # 带删除线 = 已禁用
            raw = copy.deepcopy(v)
            break
    if raw is None:
        return None
    rules = [r for r in raw[3] if r[0] not in ("cache status", "bitrate")]

    class V(object):
        pass
    v = V()
    v.name = raw[0]
    v.rules = rules
    return v


def candidates(meta):
    """抓源 + 按版本档过滤排序 + 加上"分辨率不降"的硬闸。"""
    import scraper, releases
    imdb = meta["imdb"]
    if meta["kind"] == "episode":
        alt = "(.*|S%02dE%02d|%s)" % (meta["s"], meta["e"], imdb)
    else:
        alt = "(.*|%s)" % imdb
    rs = scraper.scrape(imdb, alt)
    v = build_version()
    if v is not None:
        rs = releases.sort(rs, v, False)
    out, seen = [], set()
    for r in rs:
        res = _title_res(r)
        if res and meta["res"] and res < meta["res"]:
            continue                       # 分辨率不降
        if not r.hash:
            continue
        # 同一个发布常同时出现在多个源里(hash 相同)。不去重会把有限的尝试次数
        # 浪费在重复的片源上 —— 实测 6 次尝试里有 4 次是同两个发布。
        h = r.hash.lower()
        if h in seen:
            continue
        seen.add(h)
        out.append(r)
    return out


def _title_res(rel):
    """plex_debrid 的 resolution 正则要求带 p,`1080i` 会被解析成 0 从而绕过分辨率闸。
    隔行片源(老剧的 BluRay MPEG-2 母版)是真 1080,不能当未知放过 —— 这里补 i。"""
    try:
        v = int(rel.resolution)
    except Exception:
        v = 0
    if v:
        return v
    m = re.search(r"(2160|1080|720|480)(?=[pi])", str(rel.title), re.I)
    return int(m.group()) if m else 0


def try_replace(meta, rel, need_bytes, want_ep):
    """加新的 -> 找到目标文件 -> 体积闸 -> unrestrict 实测。失败就撤销,返回 None。"""
    st, res = rd("/torrents/addMagnet", {"magnet": "magnet:?xt=urn:btih:" + rel.hash})
    nid = res.get("id") if isinstance(res, dict) else None
    if not nid:
        return None, "addMagnet HTTP %s" % st

    def bail(reason):
        rd("/torrents/delete/" + nid, method="DELETE")
        return None, reason

    st, info = rd("/torrents/info/" + nid)
    files = info.get("files") or []
    if not files:
        return bail("无文件列表")
    # 剧集只选目标那一集;电影选所有大文件
    if want_ep:
        pat = re.compile(r"(?<![0-9])S0*%d[\s._-]*E0*%d(?![0-9])" % want_ep, re.I)
        sel = [f for f in files if pat.search(os.path.basename(f.get("path", "")))]
        if not sel:
            return bail("包里没有 S%02dE%02d" % want_ep)
    else:
        sel = [f for f in files if (f.get("bytes") or 0) > 100_000_000] or files
    target = max(sel, key=lambda f: f.get("bytes") or 0)
    if (target.get("bytes") or 0) < need_bytes * MIN_SIZE_RATIO:
        return bail("体积 %.2fGB < 原件 %.2fGB 的 %d%%"
                    % ((target.get("bytes") or 0) / 1e9, need_bytes / 1e9, MIN_SIZE_RATIO * 100))
    rd("/torrents/selectFiles/" + nid, {"files": ",".join(str(f["id"]) for f in sel)})

    ok = False
    for _ in range(8):
        st, info = rd("/torrents/info/" + nid)
        if info.get("status") == "downloaded" and info.get("links"):
            ok = True
            break
        if info.get("status") in ("error", "magnet_error", "dead", "virus"):
            break
        time.sleep(3)
    if not ok:
        return bail("未秒缓存(status=%s)" % info.get("status"))

    sel_sorted = [f for f in (info.get("files") or []) if f.get("selected")]
    try:
        pos = [f["id"] for f in sel_sorted].index(target["id"])
    except ValueError:
        pos = 0
    links = info.get("links") or []
    if pos >= len(links):
        return bail("链接数与文件数对不上")
    code, err = unrestrict_code(links[pos])
    if code != 200:
        return bail("新片源 unrestrict HTTP %s %s" % (code, err))
    return nid, "%.2fGB" % ((target.get("bytes") or 0) / 1e9)


# ---------------------------------------------------------------- 主流程
def main():
    state = load_state()
    tors = rd_torrents()
    if args.sample:
        tors = tors[:args.sample]
    total_links = sum(len(t.get("links") or []) for t in tors)
    print("扫描 %d 个种子 / %d 条链接(节流 %.1f 次/秒,约 %.1f 分钟)"
          % (len(tors), total_links, args.rate, total_links / args.rate / 60))

    bad = scan(tors, state)
    save_state(state)
    print("发现有死档的种子: %d 个" % len(bad))
    if args.scan_only or not bad:
        if not _logged[0]:
            print("(无状态变化,未写 log)")
        return

    idx = plex_index()
    done = 0
    for t, info, dead, live in bad:
        if done >= args.limit:
            log("SKIP", "已达单次上限 %d,剩余留待下轮" % args.limit)
            break
        files = [f for f in (info.get("files") or []) if f.get("selected")]
        for i in dead:
            fn = os.path.basename(files[i].get("path", "")) if i < len(files) else ""
            if args.only and args.only.lower() not in fn.lower():
                continue
            meta = idx.get(fn)
            if not meta or not meta.get("imdb"):
                log("MISS", "Plex 里查不到 %s(无法定位 imdb),跳过" % fn[:70])
                continue
            need = meta["size"] or (files[i].get("bytes") or 0)
            want_ep = (meta["s"], meta["e"]) if meta["kind"] == "episode" else None
            cands = candidates(meta)
            head = "%s %s %.2fGB %sp" % (meta["imdb"], fn[:52], need / 1e9, meta["res"])
            if not cands:
                log("NOFIX", "%s —— 三源合计 0 个合格候选" % head)
                continue
            if not args.apply:
                log("PLAN", "%s -> 候选 %d 个,首选 %s (%.2fGB, %s)"
                    % (head, len(cands), cands[0].title[:56], cands[0].size, cands[0].source))
                continue
            picked = None
            for rel in cands[:args.tries]:
                nid, why = try_replace(meta, rel, need, want_ep)
                if nid:
                    picked = (nid, rel, why)
                    break
                log("TRY", "%s 落选:%s" % (rel.title[:56], why))
            if not picked:
                log("NOFIX", "%s —— 试过的 %d 个候选都没成" % (head, min(len(cands), args.tries)))
                continue
            nid, rel, size_s = picked
            if live == 0:
                rd("/torrents/delete/" + t["id"], method="DELETE")
                log("HEAL", "%s -> %s (%s, %s);旧种全死已删"
                    % (head, rel.title[:56], size_s, rel.source))
            else:
                log("HEAL", "%s -> %s (%s, %s);旧种还有 %d 条活链接,保留"
                    % (head, rel.title[:56], size_s, rel.source, live))
            done += 1
    save_state(state)
    if not _logged[0]:
        print("(无状态变化,未写 log)")


if __name__ == "__main__":
    main()
