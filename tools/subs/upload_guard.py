# -*- coding: utf-8 -*-
"""往 Plex 上传字幕之前的两条规矩,fetch_subs(容器里)和 align_subs(宿主机)共用这一份。

1. **按版本上传**。同一集/同一部挂了两个文件(两个 Media),上传接口
   `POST /library/metadata/{rk}/subtitles` 不带 `mediaItemID` 时一律落到 `Media[0]`;
   带上 `mediaItemID=<Media.id>` 才落到指定版本(网页版的字幕搜索和挂载都带这个参数)。

2. **上传会静默顶掉同一版本里同格式的全部外挂字幕**(2026-11-02 实测):往某版本传 srt,
   该版本已有的 srt 外挂(不论是上传的 transient=0 还是从 OpenSubtitles 挂载的 transient=1)
   都会消失,服务端日志里没有删除记录;不同格式(srt 与 ass)可以并存;新传的自动成为选中。
   全库 125 份上传字幕,同版本同格式两份的情况是 0 例。所以上传前必须先看会顶掉谁 ——
   用户手工传的字幕不能被我们的自动流程顶掉。
"""

# Plex 的 codec 名:.ssa 传上去和 .ass 视作同一种(都是 ASS 家族),宁可多拦不漏拦
_FAMILY = {"ssa": "ass"}


def fmt_family(fmt):
    f = (fmt or "").lower().lstrip(".")
    return _FAMILY.get(f, f)


def clobbered(streams, fmt, keep=None):
    """往这个版本传 fmt 格式的字幕,会被 Plex 顺带顶掉的已有外挂字幕流。

    streams:该版本(Media 下所有 Part)的 Stream 列表;keep:本来就打算替换掉的那条的 id
    (对轴改写后回传时,旧的那条被顶掉正是目的)。"""
    fam = fmt_family(fmt)
    return [s for s in streams
            if s.get("streamType") == 3 and s.get("key")
            and str(s.get("id")) != str(keep) and fmt_family(s.get("codec")) == fam]


def describe(streams):
    """日志里点名会被顶掉的是哪几条:id + 语言 + 来源(上传/挂载)。"""
    return "、".join("id=%s %s %s" % (s.get("id"), s.get("languageCode") or s.get("language") or "语言未知",
                                     "挂载" if str(s.get("transient")) == "1" else "上传")
                    for s in streams)
