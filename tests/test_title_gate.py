# -*- coding: utf-8 -*-
"""
片名闸(media.deviation)单测。锁死三条性质:
  · 片名前面只许是标签/网站名/"译名 /"/分隔符 —— 别的剧名里含着这个片名不算(Smiling Friends ≠ Friends)
  · 真实片源里常见的前缀写法照样放行(俄语资源 "Друзья / Friends"、[TGx]、www.xxx.org - )
  · 怪标题不能把正则拖进指数级回溯;抓取器从正则字符串里嗅探的类型/季/集/imdb 不变

标题样本全部取自 2026-10-27/28 的 PDZURG 日志。
跑法(容器里): docker cp 仓库的 plex_debrid 和 tests 到同一目录,/venv/bin/python3 tests/test_title_gate.py
"""
import os, sys, time
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
for cand in (os.path.join(HERE, "..", "plex_debrid"), "/plex_debrid"):
    if os.path.exists(os.path.join(cand, "content", "classes.py")):
        PD = os.path.abspath(cand)
        break
else:
    raise SystemExit("找不到 plex_debrid/content/classes.py")
print("被测代码: %s" % PD)
sys.path.insert(0, PD)
os.chdir("/")  # trakt 模块导入时从当前目录的 .env 读凭据,和正式进程一样在 / 下跑

import regex
import ui  # 照 main.py 的顺序先导 ui;直接导 content 会撞上 plex_debrid 模块间的循环 import
from content import classes

fails = []


def item(**kw):
    m = classes.media(SimpleNamespace(**kw))
    m.versions = lambda *a, **k: []   # deviation() 只为副作用调它,这里用不到版本档
    m.genre = lambda: []              # 非动画
    return m


def check(name, dev, accept, reject):
    r = regex.compile(dev, regex.I)
    for t in accept:
        if not r.match(t):
            fails.append("%s: 应放行却被拒: %s" % (name, t))
    for t in reject:
        if r.match(t):
            fails.append("%s: 应拒收却放行: %s" % (name, t))


ep = item(type="episode", grandparentTitle="Friends", grandparentYear=1994, parentIndex=3, index=1,
          originallyAvailableAt="1996-09-19")
check("老友记 S03E01", ep.deviation(),
      accept=["Friends.S03E01.1080p.BluRay.x264-PSYCHD.mkv",
              "[TGx] Friends S03E01 720p",
              "./.Friends.S03E01.BDRip",
              " / Friends S03E01 (24) [1994 BDRemux 1080p]",
              "Друзья / Friends S03E01 WEB-DL",
              "..3-../.Friends.S03E01.[1996.DVDRip.ENG+RUS]",
              "www.UIndex.org    -    Friends S03E01 720p WEB",
              "【高清剧集网 www.BTHDTV.com】Friends.S03E01.1080p",
              ".Friends.S03E01..BDrip.AAC.720P.x264.mp4"],
      reject=["Smiling Friends S03E01 Silly Samuel 1080P Amzn Web Dl Ddp5 1 H 264.m4v",
              "Thomas the Tank Engine and Friends S03E01. A Scarf for Percy.mp4",
              "LEGO.Friends.S03E01.720p.AMZN.WEBRip.x264-GalaxyTV.mkv",
              "Your.Friends.and.Neighbors.S03E01.720p.WEB.h264.mkv",
              "Talking.Tom.and.Friends.S03E01.mkv"])

season = item(type="season", parentTitle="Friends", parentYear=1994, index=6)
check("老友记 第 6 季", season.deviation(),
      accept=["Friends.S06.1080p.BluRay.x264", "./.Friends.[S06].(1999).WEB-DLRip-HEVC.1080p..RIPS.CLUB.|.P",
              "[moon] Friends S06 1080p"],
      reject=["Smiling.Friends.S06.1080p.AMZN.WEB-DL", "Super.Friends.S06.DVDRip"])

show = item(type="show", title="Friends", year=1994)
check("老友记 整剧", show.deviation(),
      accept=["Friends.1994.Complete.Series.BDRip", "Friends.S01-S10.1080p.BluRay"],
      reject=["Just Good Friends S01-S03 DVDRip", "Thomas.and.Friends.Complete.Series"])

# 带国家后缀的剧名:aliases() 要补一个不带后缀的写法,别名经季传到集
office_ep = item(type="episode", grandparentTitle="The Office (US)", grandparentYear=2005, parentIndex=2, index=19,
                 originallyAvailableAt="2006-04-27")
office_season = item(type="season", parentTitle="The Office (US)", parentYear=2005, index=2, Episodes=[office_ep])
office = item(type="show", title="The Office (US)", year=2005, Seasons=[office_season])
office.aliases('en')
check("办公室 S02E19", office_ep.deviation(),
      accept=["The.Office.US.S02E19.Michaels.Birthday.1080p.BluRay.DDP5.1.x264-BTN.mkv",
              "The.Office.S02E19.720p.BluRay.nHD.x264-NhaNc3.mkv",
              "The Office US S02E19  720p BluRay x264 UnIon mp4"],
      reject=["The.Office.UK.S02E19.720p.mkv", "The.Office.2001.S02E19.DVDRip"])

movie = item(type="movie", title="Trainspotting", year=1996)
check("猜火车", movie.deviation(),
      accept=["Trainspotting.1996.1080p.BluRay.x264", "[YTS] Trainspotting (1996) 720p"],
      reject=["T2.Trainspotting.2017.1080p.BluRay"])

# 怪标题:没有一个能让单次匹配超过 50ms(指数级回溯会是秒级甚至卡死)
for t in [" / " * 40 + "x", "[ ] " * 40 + "x", " 1 /" * 40 + "x", "./" * 80 + "x", "www.a." * 40 + "x"]:
    for dev in (ep.deviation(), movie.deviation()):
        t0 = time.perf_counter()
        regex.match(dev, t, regex.I)
        dt = time.perf_counter() - t0
        if dt > 0.05:
            fails.append("回溯过慢 %.0fms: %r" % (dt * 1000, t[:40]))


def sniff(q):
    """照抄 torrentio/comet 从 altquery 里嗅探的四样东西"""
    f = lambda p: (regex.search(p, q, regex.I).group() if regex.search(p, q, regex.I) else None)
    return ("show" if regex.search(r'(S[0-9]|complete|S\?[0-9])', q, regex.I) else "movie",
            f(r'(?<=S)([0-9]+)'), f(r'(?<=E)([0-9]+)'), f(r'(tt[0-9]+)'))


if sniff(movie.deviation() + "(tt0117951)?") != ("movie", None, None, "tt0117951"):
    fails.append("电影的嗅探结果变了: %r" % (sniff(movie.deviation() + "(tt0117951)?"),))
if sniff(ep.deviation())[:3] != ("show", "03", "01"):
    fails.append("单集的嗅探结果变了: %r" % (sniff(ep.deviation()),))
if sniff(classes.title_prefix) != ("movie", None, None, None):
    fails.append("前缀本身含有会被嗅探的子串: %r" % (sniff(classes.title_prefix),))

if fails:
    print("FAIL %d 条:" % len(fails))
    for f in fails:
        print("  " + f)
    sys.exit(1)
print("OK 片名闸全部通过")
