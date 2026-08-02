# -*- coding: utf-8 -*-
"""清掉重复的外挂字幕(同一条目下标题相同的只留一条,优先留选中的)。默认干跑。"""
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


def norm(t):
    return str(t or "").replace(" - 複製", "").replace(" - 副本", "").strip().lower()


total = 0
for sec in ("1", "2"):
    q = "/library/sections/%s/all%s" % (sec, "?type=4" if sec == "2" else "")
    for it in (px("GET", q)[1].get("Metadata") or []):
        md = (px("GET", "/library/metadata/%s" % it["ratingKey"])[1].get("Metadata") or [{}])[0]
        ext = []
        for m in md.get("Media") or []:
            for p in m.get("Part") or []:
                ext += [s for s in (p.get("Stream") or []) if s.get("streamType") == 3 and s.get("key")]
        groups = {}
        for s in ext:
            groups.setdefault(norm(s.get("title")), []).append(s)
        for key, g in groups.items():
            if len(g) < 2:
                continue
            # **保留最旧的那条**(id 最小 = 先到的)。同名重复里可能混着用户自己手动传的
            # (实测瑞克和莫蒂 S02E08 的 23717 就是),按"选中优先/新的优先"排会把它删掉。
            g.sort(key=lambda s: int(s.get("id") or 0))
            keep, drop = g[0], g[1:]
            nm = md.get("title") or ""
            if md.get("grandparentTitle"):
                nm = "%s S%02dE%02d" % (md["grandparentTitle"], md.get("parentIndex") or 0,
                                        md.get("index") or 0)
            print("%-28s 保留 id=%-7s 删 %s  (%s)"
                  % (nm[:28], keep["id"], [s["id"] for s in drop], str(keep.get("title"))[:34]))
            total += len(drop)
            if APPLY:
                for s in drop:
                    px("DELETE", "/library/streams/%s" % s["id"])
print("\n重复共 %d 条%s" % (total, "(已删除)" if APPLY else " —— 干跑,加 --apply 执行"))
