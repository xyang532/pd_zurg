# -*- coding: utf-8 -*-
"""去重可能删掉了当时被选中的那条,导致有字幕却不显示。把未选中的补选上。默认干跑。"""
import json, io, os, sys, urllib.request, urllib.error
APPLY = "--apply" in sys.argv
cfg = json.load(io.open("/config/settings.json", encoding="utf-8"))
BASE = cfg["Plex server address"]; TOK = os.environ.get("PLEX_TOKEN") or cfg["Plex users"][0][1]


def px(m, p):
    u = "%s%s%sX-Plex-Token=%s" % (BASE, p, "&" if "?" in p else "?", TOK)
    r = urllib.request.Request(u, headers={"Accept": "application/json"})
    r.get_method = lambda: m
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            b = resp.read()
            return resp.status, (json.loads(b)["MediaContainer"] if b.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, {}


def is_zhish(s):
    t = "%s%s" % (s.get("language") or "", s.get("languageTag") or "")
    return ("中文" in t) or ("zh" in t.lower()) or ("chi" in t.lower()) or not t.strip()


n = 0
for sec in ("1", "2"):
    q = "/library/sections/%s/all%s" % (sec, "?type=4" if sec == "2" else "")
    for it in (px("GET", q)[1].get("Metadata") or []):
        md = (px("GET", "/library/metadata/%s" % it["ratingKey"])[1].get("Metadata") or [{}])[0]
        for m in md.get("Media") or []:
            for p in m.get("Part") or []:
                subs = [s for s in (p.get("Stream") or []) if s.get("streamType") == 3]
                if any(s.get("selected") for s in subs):
                    continue
                cand = [s for s in subs if s.get("key") and is_zhish(s)]
                if not cand:
                    continue
                nm = md.get("title") or ""
                if md.get("grandparentTitle"):
                    nm = "%s S%02dE%02d" % (md["grandparentTitle"],
                                            md.get("parentIndex") or 0, md.get("index") or 0)
                print("%-30s 无选中字幕 -> 选 id=%s %s"
                      % (nm[:30], cand[0]["id"], str(cand[0].get("title"))[:34]))
                n += 1
                if APPLY:
                    st, _ = px("PUT", "/library/parts/%s?subtitleStreamID=%s&allParts=1"
                               % (p["id"], cand[0]["id"]))
                    if st not in (200, 204):
                        print("     选中失败 HTTP %s" % st)
print("\n有中文字幕但未选中的: %d 个%s" % (n, "(已处理)" if APPLY else " —— 干跑"))
