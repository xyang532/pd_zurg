# -*- coding: utf-8 -*-
"""
watchlist_prune —— 关注列表里已经抓齐、确认能播的条目自动移出;没抓齐的留着让 plex_debrid 继续抓。

跑在 pd_zurg 容器里(要用容器网络里的 Plex 地址和 settings.json 里的 Plex 令牌)。默认干跑,--apply 才移除。
由 tools/autoheal/autoheal.sh 在每天 04:00 的死源自愈之后调用(先修死档,再判断能不能播)。

判据(2026-10-28 定案):
  · 电影:本地库里有,且文件头/中/尾各读 1 MiB 都读满
  · 剧集:已完结、正片(第 1 季起)每季已播出的集都在库里、每季抽一集读中段 1 MiB 读满
  · 移除时看 plex.tv 的返回码,再重新拉一次关注列表确认真没了;任何一步失败记 ERROR,条目留着

为什么这么设计:
  · **完结要两个来源都同意**。plex.tv 只给连载中的剧标 isContinuingSeries,完结剧是"没有这个字段"——
    光凭缺字段判完结太脆,所以再问 TVmaze(免费、不要密钥)。误删的代价是以后的新集没人抓,
    误留只是多留一条,所以两边不一致一律留。trakt 也能查,但 /.env 里的凭据实测返回 403。
  · **能播 = 真读到字节**。在库只说明 Plex 扫到过文件;RD 那边下架了,文件名还在,读才会失败。
    读走 Plex 自己的文件接口(和直放同一条路),每次最多读 1 MiB + 1 字节,服务器无视 Range 也不会拉整个文件。
  · plex_debrid 自带的移除:电影是 RD 一接种子就移(不验证能播),剧集只看 hasended(),
    而 plex.tv 对完结剧不给那个字段,所以完结剧永远不会被它移掉。这个脚本补的是后者,并给前者加一道验证。
"""
import argparse, collections, datetime, json, os, socket, sys, time
import urllib.request, urllib.parse, urllib.error

SETTINGS = "/config/settings.json"
LOG_PATH = "/log/watchlist_prune.log"
REPORT_PATH = "/log/watchlist_prune_last.json"
DISCOVER = "https://discover.provider.plex.tv"
TVMAZE = "https://api.tvmaze.com/lookup/shows?imdb="
PAGE = 100                # plex.tv 每页上限 100,传更大会 400
READ_BYTES = 1048576      # 每处读 1 MiB
READ_TIMEOUT = 120        # RD 冷启动可能要几十秒

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true", help="真的从关注列表移除(默认干跑,只记录会怎么做)")
ap.add_argument("--only", default="", help="只处理片名含该子串的条目(调试用)")
args = ap.parse_args()

S = json.load(open(SETTINGS))
TOKEN = S["Plex users"][0][1]
PMS = S["Plex server address"].rstrip("/")
LOG = []


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(kind, msg):
    """每行立刻落盘:中途崩了,前面的判定也还在"""
    line = "%s %-8s%s" % (stamp(), kind, msg)
    LOG.append(line)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print("写日志失败(%s): %s" % (type(e).__name__, e), file=sys.stderr)


class Fail(Exception):
    """分档:timeout / http <码> / error <类型>。混成一档会把"网络不通"读成"没有这个东西"。"""


def tier(e):
    if isinstance(e, urllib.error.HTTPError):
        return "http %d" % e.code
    if isinstance(e, (socket.timeout, TimeoutError)) or "timed out" in str(e):
        return "timeout"
    return "error %s" % type(e).__name__


def request(url, method="GET", plex=True, rng=None, timeout=60, limit=None):
    """返回 (状态码, 正文字节, 响应头)。令牌放请求头不放 URL,出错信息里不会带出令牌。"""
    h = {"Accept": "application/json"}
    if plex:
        h["X-Plex-Token"] = TOKEN
    if rng:
        h["Range"] = rng
    # 不带正文的 PUT 也要发 Content-Length: 0,否则有的服务器回 411
    req = urllib.request.Request(url, headers=h, method=method, data=None if method == "GET" else b"")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(limit) if limit else r.read(), dict(r.headers)
    except Exception as e:
        raise Fail(tier(e)) from e


def get_json(url, plex=True):
    return json.loads(request(url, plex=plex)[1])


def paged(url):
    out, start = [], 0
    while True:
        sep = "&" if "?" in url else "?"
        mc = get_json("%s%sX-Plex-Container-Size=%d&X-Plex-Container-Start=%d" % (url, sep, PAGE, start))["MediaContainer"]
        got = mc.get("Metadata", [])
        out += got
        start += len(got)
        if not got or start >= int(mc.get("totalSize", mc.get("size", 0))):
            return out


def watchlist():
    return paged(DISCOVER + "/library/sections/watchlist/all")


def local_item(guid):
    """本地库里 guid 相同的条目(plex.tv 的 ratingKey 就是本地 guid plex://movie/<rk> 的尾巴)"""
    hits = get_json(PMS + "/library/all?guid=" + urllib.parse.quote(guid, safe=""))["MediaContainer"].get("Metadata", [])
    return hits[0] if hits else None


def readable(rating_key, where):
    """读这个条目第一个文件在 where(0~1 的比例列表)处各 1 MiB。返回 None 表示都读满,否则返回原因。"""
    md = get_json(PMS + "/library/metadata/%s" % rating_key)["MediaContainer"]["Metadata"][0]
    try:
        part = md["Media"][0]["Part"][0]
    except (KeyError, IndexError):
        return "库里没有文件记录"
    size = int(part.get("size", 0))
    if size <= READ_BYTES:
        return "文件只有 %d 字节" % size
    for frac in where:
        off = min(int(size * frac), size - READ_BYTES)
        try:
            st, data, _ = request(PMS + part["key"], rng="bytes=%d-%d" % (off, off + READ_BYTES - 1),
                                  timeout=READ_TIMEOUT, limit=READ_BYTES + 1)
        except Fail as e:
            return "读 %d%% 处失败(%s)" % (frac * 100, e)
        if st != 206:
            return "读 %d%% 处返回 %d 而不是 206(服务器没按范围给)" % (frac * 100, st)
        if len(data) != READ_BYTES:
            return "读 %d%% 处只读到 %d/%d 字节" % (frac * 100, len(data), READ_BYTES)
    return None


def judge_movie(m):
    loc = local_item(m["guid"])
    if not loc:
        return "keep", "不在库里", {}
    why = readable(loc["ratingKey"], (0.0, 0.5, 1.0))
    if why:
        return "keep", "在库但读不出:" + why, {"local": loc["ratingKey"]}
    return "remove", "在库,头/中/尾各读 1 MiB 都读满", {"local": loc["ratingKey"]}


def ended(md):
    """(是否完结, 说明)。两个来源都说完结才算。"""
    if md.get("isContinuingSeries"):
        return False, "plex.tv 标为连载中"
    imdb = next((g["id"].split("://")[1] for g in md.get("Guid", []) if g["id"].startswith("imdb://")), None)
    if not imdb:
        return False, "plex.tv 没给 imdb 号,没法问 TVmaze,按未完结处理"
    try:
        status = get_json(TVMAZE + imdb, plex=False).get("status")
    except Fail as e:
        return False, "TVmaze 查不到(%s),按未完结处理" % e
    if status != "Ended":
        return False, "plex.tv 没标连载中,但 TVmaze 说 %s,两边不一致按未完结处理" % status
    return True, "plex.tv 未标连载中且 TVmaze 说 Ended"


def judge_show(m):
    md = get_json(DISCOVER + "/library/metadata/%s?includeGuids=1" % m["ratingKey"])["MediaContainer"]["Metadata"][0]
    done, why = ended(md)
    if not done:
        return "keep", "未完结:" + why, {}
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    expected = set()
    for season in paged(DISCOVER + "/library/metadata/%s/children" % m["ratingKey"]):
        if int(season.get("index", 0)) < 1:          # 第 0 季是花絮/特别篇,不算正片
            continue
        for ep in paged(DISCOVER + "/library/metadata/%s/children" % season["ratingKey"]):
            # 完结剧不该有没播的集;没有日期的也算进应有,宁可多留不错删
            if ep.get("originallyAvailableAt", "0000") <= today:
                expected.add((int(ep["parentIndex"]), int(ep["index"])))
    if not expected:
        return "keep", "plex.tv 上一集正片都没列出来,没法判断齐不齐", {}
    loc = local_item(m["guid"])
    if not loc:
        return "keep", "已完结,但整部剧都不在库里(应有 %d 集)" % len(expected), {}
    leaves = get_json(PMS + "/library/metadata/%s/allLeaves" % loc["ratingKey"])["MediaContainer"].get("Metadata", [])
    have = {(int(e["parentIndex"]), int(e["index"])): e for e in leaves if "parentIndex" in e and "index" in e}
    missing = sorted(expected - set(have))
    detail = {"local": loc["ratingKey"], "expected": len(expected), "have": len(expected & set(have))}
    if missing:
        by_season = collections.Counter(s for s, _ in missing)
        detail["missing"] = ["S%02dE%02d" % x for x in missing]
        return "keep", "已完结,缺 %d/%d 集:%s" % (len(missing), len(expected),
                                                "、".join("第 %d 季 %d 集" % kv for kv in sorted(by_season.items()))), detail
    for s in sorted({s for s, _ in expected}):
        eps = sorted(i for (ss, i) in expected if ss == s)
        pick = have[(s, eps[len(eps) // 2])]   # 每季抽中间那集
        why = readable(pick["ratingKey"], (0.5,))
        if why:
            return "keep", "已完结、集数齐,但第 %d 季第 %d 集读不出:%s" % (s, eps[len(eps) // 2], why), detail
    return "remove", "已完结、正片 %d 集都在库、每季抽一集都读满" % len(expected), detail


def remove(m):
    url = DISCOVER + "/actions/removeFromWatchlist?ratingKey=" + urllib.parse.quote(m["ratingKey"])
    try:
        st, body, _ = request(url, method="PUT")
    except Fail as e:
        return "plex.tv 移除请求失败(%s)" % e
    if not 200 <= st < 300:
        return "plex.tv 移除返回 %d:%s" % (st, body[:200].decode("utf-8", "replace"))
    return None


def main():
    t0 = time.time()
    try:
        items = watchlist()
    except Fail as e:
        log("ABORT", "拉不到关注列表(%s),本轮什么都没做。令牌失效的话看 pd_zurg AGENT_NOTES 的令牌恢复步骤" % e)
        return 1
    report = {"run_utc": stamp(), "apply": args.apply, "watchlist_size": len(items), "items": []}
    tally = collections.Counter()
    removed = []
    for m in items:
        if args.only and args.only.lower() not in m.get("title", "").lower():
            continue
        name = "%s (%s, %s)" % (m.get("title"), m.get("year"), m.get("type"))
        try:
            if m.get("type") == "movie":
                verdict, why, detail = judge_movie(m)
            elif m.get("type") == "show":
                verdict, why, detail = judge_show(m)
            else:
                verdict, why, detail = "keep", "类型 %s 不处理" % m.get("type"), {}
        except Fail as e:
            verdict, why, detail = "error", "检查时网络出错(%s),本轮留着" % e, {}
        except Exception as e:
            verdict, why, detail = "error", "检查时异常 %s: %s,本轮留着" % (type(e).__name__, e), {}
        if verdict == "remove":
            if args.apply:
                err = remove(m)
                if err:
                    verdict, why = "error", why + ";但" + err
                else:
                    removed.append(m)
            else:
                verdict = "would-remove"
        kind = {"remove": "REMOVED", "would-remove": "DRYRUN", "keep": "KEEP", "error": "ERROR"}[verdict]
        log(kind, "%s —— %s" % (name, why))
        tally[verdict] += 1
        report["items"].append({"title": m.get("title"), "year": m.get("year"), "type": m.get("type"),
                                "ratingKey": m.get("ratingKey"), "verdict": verdict, "why": why, **detail})
    if removed:
        try:
            still = {x["ratingKey"] for x in watchlist()} & {m["ratingKey"] for m in removed}
        except Fail as e:
            still = set()
            log("ERROR", "移除后重新拉关注列表失败(%s),没法确认是否真移掉了" % e)
            tally["error"] += 1
        for m in removed:
            if m["ratingKey"] in still:
                log("ERROR", "%s —— plex.tv 返回成功但它还在关注列表里" % m.get("title"))
                tally["remove"] -= 1
                tally["error"] += 1
    summary = "检查 %d 条:移出 %d、干跑会移出 %d、保留 %d、出错 %d,用时 %.0fs" % (
        len(report["items"]), tally["remove"], tally["would-remove"], tally["keep"], tally["error"], time.time() - t0)
    log("SUMMARY", summary)
    report["summary"] = summary
    report["log"] = LOG
    try:
        with open(REPORT_PATH + ".tmp", "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        os.replace(REPORT_PATH + ".tmp", REPORT_PATH)
    except Exception as e:
        log("ERROR", "写报告 %s 失败(%s): %s" % (REPORT_PATH, type(e).__name__, e))
        return 1
    return 1 if tally["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
