# -*- coding: utf-8 -*-
"""全库普查:哪些片子没有中文字幕、哪些挂着可疑来源的中文字幕。只读。"""
import json, io, os, re, urllib.request, urllib.error

cfg = json.load(io.open("/config/settings.json", encoding="utf-8"))
BASE = cfg["Plex server address"]; TOK = os.environ.get("PLEX_TOKEN") or cfg["Plex users"][0][1]
BAD = re.compile(r"(?<![A-Za-z0-9])(TELESYNC|TS|CAM|HDTS|HDCAM|SCREENER|SCR|WORKPRINT)(?![A-Za-z0-9])", re.I)


def px(p):
    u = "%s%s%sX-Plex-Token=%s" % (BASE, p, "&" if "?" in p else "?", TOK)
    return json.loads(urllib.request.urlopen(
        urllib.request.Request(u, headers={"Accept": "application/json"}), timeout=120).read())["MediaContainer"]


def zh(s):
    l = (s.get("language") or "") + (s.get("languageTag") or "") + (s.get("languageCode") or "")
    if ("中文" in l) or ("zh" in l.lower()) or ("chi" in l.lower()):
        return True
    # **上传上去的字幕 Plex 不打语言标记**(lang=None),按 language 统计会把它们全漏掉 ——
    # 实测上传 34 条之后,普查数字反而"倒退"了。外挂且语言未知的一律计入。
    return bool(s.get("key")) and not l.strip()


stats = {"无中文": [], "内嵌中文": 0, "外挂可疑": [], "外挂正常": 0}
for sec, typ in (("1", None), ("2", "4")):
    q = "/library/sections/%s/all%s" % (sec, "?type=" + typ if typ else "")
    for it in (px(q).get("Metadata") or []):
        rk = it["ratingKey"]
        md = (px("/library/metadata/%s" % rk).get("Metadata") or [{}])[0]
        name = md.get("title") or ""
        if md.get("grandparentTitle"):
            name = "%s S%02dE%02d" % (md["grandparentTitle"], md.get("parentIndex") or 0, md.get("index") or 0)
        subs = []
        for m in md.get("Media") or []:
            for p in m.get("Part") or []:
                subs += [s for s in (p.get("Stream") or []) if s.get("streamType") == 3]
        zsub = [s for s in subs if zh(s)]
        if not zsub:
            stats["无中文"].append(name)
        elif any(not s.get("key") for s in zsub):
            stats["内嵌中文"] += 1
        else:
            t = " ".join(str(s.get("title") or "") for s in zsub)
            if BAD.search(t):
                stats["外挂可疑"].append("%s  <- %s" % (name, t[:60]))
            else:
                stats["外挂正常"] += 1

print("== 全库中文字幕现状 ==")
print("  有内嵌中文字幕      : %d" % stats["内嵌中文"])
print("  外挂中文、来源正常  : %d" % stats["外挂正常"])
print("  外挂中文、**枪版类** : %d" % len(stats["外挂可疑"]))
for x in stats["外挂可疑"][:15]:
    print("      %s" % x)
print("  完全没有中文字幕    : %d" % len(stats["无中文"]))
for x in stats["无中文"][:12]:
    print("      %s" % x)
