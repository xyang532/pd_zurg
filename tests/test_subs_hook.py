# -*- coding: utf-8 -*-
"""subs_hook 的 webhook 载荷解析单测。

**这套断言的存在理由是一次真实的假绿**:第一版解析器用手写的 multipart 载荷测过、通过,
上线后 Plex 真发来的第一个事件就解不开 —— 因为真实载荷的 payload 分段比我构造的多一行
`Content-Type: application/json`。所以下面的字节形状**照抄 Plex 实际发的那一份**
(2026-08-04 从 log/bad_webhook.bin 抓的),不许"看着差不多"地重写。
"""
import io, os, sys, json
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
for cand in (os.path.join(HERE, "..", "tools", "subs", "subs_hook.py"),
             os.path.join(HERE, "subs_hook.py"), "/tmp/subs_hook.py"):
    if os.path.exists(cand):
        TARGET = cand
        break
else:
    raise SystemExit("找不到 subs_hook.py")
print("被测模块: %s" % TARGET)
spec = importlib.util.spec_from_file_location("sh", TARGET)
sh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sh)

fails = []


def check(name, ok, extra=""):
    print("  %-4s %s%s" % ("OK" if ok else "FAIL", name, ("  " + extra) if extra else ""))
    if not ok:
        fails.append(name)


BOUND = "------------------------okEvLNvSFTd82BNCLd5R9o"
CT = "multipart/form-data; boundary=%s" % BOUND


def plex_body(payload, thumb=b"\xff\xd8\xff\xe0JUNK\x00\r\n--fake--\x89PNG", with_ct=True):
    """照抄 Plex 真发的形状:payload 分段带 Content-Type,后面还挂一段二进制缩略图。

    thumb 里故意埋了 `\\r\\n--` 和 CRLF —— 纯正则切分很容易被这种字节骗到。
    """
    j = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    head = b'--' + BOUND.encode() + b'\r\n' \
           b'Content-Disposition: form-data; name="payload"\r\n'
    if with_ct:
        head += b'Content-Type: application/json\r\n'
    head += b'\r\n' + j + b'\r\n'
    tail = b'--' + BOUND.encode() + b'\r\n' \
           b'Content-Disposition: form-data; name="thumb"; filename="thumb.jpg"\r\n' \
           b'Content-Type: application/octet-stream\r\n\r\n' + thumb + b'\r\n' \
           b'--' + BOUND.encode() + b'--\r\n'
    return head + tail


NEWEV = {"event": "library.new", "user": True,
         "Metadata": {"ratingKey": "3198", "type": "movie", "title": "X圣治",
                      "librarySectionTitle": "电影"}}

# —— 生产形状:payload 分段带 Content-Type ——
d = sh.parse_payload(plex_body(NEWEV), CT)
check("真实形状(payload 段带 Content-Type)能解开", d is not None and d.get("event") == "library.new")
check("能取到 ratingKey", d is not None and d["Metadata"]["ratingKey"] == "3198",
      repr(d["Metadata"]["ratingKey"]) if d else "None")
check("中文标题不乱码", d is not None and d["Metadata"]["title"] == "X圣治")

# —— 旧形状(没有那行 Content-Type)也不能退化 ——
d2 = sh.parse_payload(plex_body(NEWEV, with_ct=False), CT)
check("没有 Content-Type 行的形状仍能解开", d2 is not None and d2.get("event") == "library.new")

# —— 二进制分段里含 \r\n-- 时不能被骗 ——
d3 = sh.parse_payload(plex_body(NEWEV, thumb=b"A\r\n--" + BOUND.encode()[:10] + b"B"), CT)
check("缩略图里含 CRLF-- 也不串段", d3 is not None and d3["Metadata"]["ratingKey"] == "3198")

# —— boundary 带引号(RFC 允许)——
d4 = sh.parse_payload(plex_body(NEWEV), 'multipart/form-data; boundary="%s"' % BOUND)
check("boundary 带引号也认", d4 is not None and d4.get("event") == "library.new")

# —— 纯 JSON 请求体(手动/自测用)——
d5 = sh.parse_payload(json.dumps(NEWEV, ensure_ascii=False).encode("utf-8"), "application/json")
check("application/json 请求体能解开", d5 is not None and d5.get("event") == "library.new")

# —— 垃圾输入必须返回 None 而不是抛异常 ——
for name, body, ct in (("空体", b"", CT),
                       ("没有 payload 分段", b"--x\r\nContent-Disposition: form-data; "
                                            b'name="thumb"\r\n\r\nzz\r\n--x--\r\n', CT),
                       ("根本不是 multipart", b"hello world", "text/plain")):
    try:
        got = sh.parse_payload(body, ct)
        check("%s -> None 不抛异常" % name, got is None, repr(got))
    except Exception as e:
        check("%s -> None 不抛异常" % name, False, "抛了 %s" % e)

# —— expand():剧/季要展开成单集,电影原样返回 ——
sh.px = lambda path, timeout=60: {"Metadata": [{"ratingKey": 11}, {"ratingKey": 12}]}
check("movie 不展开", sh.expand("3198", "movie") == ["3198"])
check("show 展开成单集", sh.expand("7", "show") == ["11", "12"])
check("season 展开成单集", sh.expand("7", "season") == ["11", "12"])
sh.px = lambda path, timeout=60: {}
check("展开不出来时退回自身", sh.expand("7", "show") == ["7"])

print("\n%s" % ("全部通过" if not fails else "失败: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
