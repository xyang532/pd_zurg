# -*- coding: utf-8 -*-
"""subs_one —— 手动给指定的片子配字幕并对轴。

老片一律不自动动(用户 2026-08-03 定),要补哪部就跑这个。用法:

    sudo python3 subs_one.py 爱乐之城              # 按片名子串,可给多个
    sudo python3 subs_one.py 寂静的朋友 不十分好莱坞
    sudo python3 subs_one.py --rk 3198,3201        # 直接给 ratingKey
    sudo python3 subs_one.py 办公室 --sections 2   # 剧集在 2 区,会展开到每一集
    sudo python3 subs_one.py 爱乐之城 --dry        # 只看会动哪些条目,不真跑

**四步实现不在这个文件里** —— 直接调 subs_hook.handle(),跟新片入库走的是同一份代码。
这里只负责三件事:把片名解析成 ratingKey、把"会动哪些"先摆出来给人看、跑完把日志里
新增的那几行打出来(判据是可观测产物,不是"没报错")。

手动触发一律带 recheck:老片在 align_state.json 里已记"量过了",不强制就整部被跳过。
"""
import io, os, sys, argparse, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("subs_hook", os.path.join(HERE, "subs_hook.py"))
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)

# 一次手动补齐超过这个数,先让人确认 —— `--rk` 打错、或片名子串太宽(比如一个"的")
# 会一口气动掉半个库,而每部都要读两个 300 秒窗口。
MAX_AUTO = 12


def leaves(rk, typ):
    """剧/季展开成单集;电影原样。"""
    return hook.expand(rk, typ)


def resolve(patterns, sections):
    hits = []
    for sec in sections:
        d = hook.px("/library/sections/%s/all" % sec)
        for m in d.get("Metadata") or []:
            t = m.get("title") or ""
            if any(p in t for p in patterns):
                hits.append((str(m.get("ratingKey")), t, m.get("type")))
    return hits


def subs_of(rk):
    d = hook.px("/library/metadata/%s" % rk)
    md = (d.get("Metadata") or [{}])[0]
    out = []
    for m in md.get("Media") or []:
        for pt in m.get("Part") or []:
            for s in pt.get("Stream") or []:
                if s.get("streamType") == 3:
                    out.append({"id": s.get("id"), "ext": bool(s.get("key")),
                                "sel": bool(s.get("selected")),
                                "lang": s.get("language") or s.get("languageCode") or "?",
                                "title": (s.get("title") or "")[:28]})
    return out


def show_subs(tag, rows):
    if not rows:
        print("    %s: 一条字幕轨都没有" % tag)
        return
    print("    %s: %d 条,选中 %d 条" % (tag, len(rows), sum(1 for r in rows if r["sel"])))
    for r in rows:
        print("      id=%-6s %s %s %-8s %s"
              % (r["id"], "外挂" if r["ext"] else "内嵌",
                 "★选中" if r["sel"] else "  ", r["lang"], r["title"]))


def main():
    ap = argparse.ArgumentParser(description="手动给指定影片配字幕并对轴")
    ap.add_argument("patterns", nargs="*", help="片名子串(可多个)")
    ap.add_argument("--rk", default="", help="直接给 ratingKey,逗号分隔")
    ap.add_argument("--sections", default="1,2", help="Plex 分区号(默认 1电影,2电视节目)")
    ap.add_argument("--dry", action="store_true", help="只列出会动哪些条目,不真跑")
    ap.add_argument("--yes", action="store_true", help="条目数超过上限也照跑")
    ap.add_argument("--no-recheck", action="store_true",
                    help="不强制重量对轴(量过的老片会被跳过,一般不要用)")
    a = ap.parse_args()

    if not a.patterns and not a.rk:
        ap.error("给个片名子串,或者 --rk")

    targets = []                                   # [(rk, 显示名)]
    if a.rk:
        for rk in [x.strip() for x in a.rk.split(",") if x.strip()]:
            d = hook.px("/library/metadata/%s" % rk)
            md = (d.get("Metadata") or [{}])[0]
            if not md:
                print("!! rk=%s 查不到,跳过" % rk)
                continue
            for one in leaves(rk, md.get("type")):
                targets.append((one, md.get("title") or one))
    if a.patterns:
        hits = resolve(a.patterns, [s.strip() for s in a.sections.split(",") if s.strip()])
        if not hits:
            print("没有片名含 %s 的条目(查的是分区 %s)" % ("/".join(a.patterns), a.sections))
            return 1
        for rk, title, typ in hits:
            got = leaves(rk, typ)
            for one in got:
                targets.append((one, title if len(got) == 1 else "%s (rk=%s)" % (title, one)))

    seen, uniq = set(), []
    for rk, name in targets:
        if rk not in seen:
            seen.add(rk)
            uniq.append((rk, name))

    print("会动这 %d 个条目:" % len(uniq))
    for rk, name in uniq:
        print("  rk=%-6s %s" % (rk, name))
    if len(uniq) > MAX_AUTO and not a.yes:
        print("\n超过 %d 个了。每部要读两个 300 秒窗口(约 90 秒 / 2.4GB),"
              "确认无误请加 --yes,或把片名写得更具体。" % MAX_AUTO)
        return 2
    if a.dry:
        print("\n--dry,到此为止。")
        return 0

    for rk, name in uniq:
        print("\n=== %s (rk=%s) ===" % (name, rk))
        show_subs("改前", subs_of(rk))
        try:
            size = os.path.getsize(hook.LOG_PATH)
        except OSError:
            size = 0
        hook.handle(rk, recheck=not a.no_recheck, why="手动")
        show_subs("改后", subs_of(rk))
        # 四个工具都往同一个 subs.log 写,把这轮新增的行打出来 —— ALIGNED / SHIFTED /
        # NOREF / UNSURE 这些判词只在那里,不打出来等于让人自己去翻日志
        try:
            with io.open(hook.LOG_PATH, encoding="utf-8", errors="replace") as f:
                f.seek(size)
                new = [l.rstrip("\n") for l in f if l.strip()]
            if new:
                print("    日志:")
                for l in new:
                    print("      %s" % l)
        except (OSError, IOError):
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
