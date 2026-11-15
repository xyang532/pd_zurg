# -*- coding: utf-8 -*-
"""upload_guard 单测:上传会顶掉同版本同格式的外挂字幕(2026-11-02 实测),闸要拦准。"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
for cand in (os.path.join(HERE, "..", "tools", "subs"), HERE, "/tmp"):
    if os.path.exists(os.path.join(cand, "upload_guard.py")):
        sys.path.insert(0, cand)
        break
else:
    raise SystemExit("找不到 upload_guard.py")
import upload_guard as U

# 第 3 条:末条覆盖闸
assert U.tail_too_far(7818, 3749), "致命魔术 CD1(末条 1:02:29,片长 130 分)必须拦"
assert U.tail_too_far(5186, 2598), "越狱 S04E02 挂 S04E05(覆盖 50%)必须拦"
assert not U.tail_too_far(7818, 7498), "整片字幕(片尾演职员表 5 分钟)要放行"
assert not U.tail_too_far(9000, 9000 - 840), "大片片尾字幕 14 分钟要放行(哈利波特实测)"
assert not U.tail_too_far(0, 100), "片长未知不判"
assert not U.tail_too_far(1000, 750) and U.tail_too_far(1000, 749), "边界:正好 75% 放行"
print("ok  tail_too_far 6 条")

fails = []


def check(name, ok, extra=""):
    print("  %-4s %s%s" % ("OK" if ok else "FAIL", name, ("  " + extra) if extra else ""))
    if not ok:
        fails.append(name)


EMB = {"streamType": 3, "id": 1, "codec": "srt", "language": "English"}            # 内嵌:没有 key
UP_SRT = {"streamType": 3, "id": 2, "key": "/library/streams/2", "codec": "srt", "transient": "0"}
MNT_SRT = {"streamType": 3, "id": 3, "key": "/library/streams/3", "codec": "srt",
           "languageCode": "zho", "transient": "1"}
UP_SSA = {"streamType": 3, "id": 4, "key": "/library/streams/4", "codec": "ssa", "transient": "0"}
AUD = {"streamType": 2, "id": 5, "key": "/x", "codec": "srt"}
ALL = [EMB, UP_SRT, MNT_SRT, UP_SSA, AUD]

ids = lambda xs: sorted(s["id"] for s in xs)
check("传 srt:顶掉上传的和挂载的 srt 外挂,不算内嵌轨、不算非字幕流", ids(U.clobbered(ALL, "srt")) == [2, 3],
      str(ids(U.clobbered(ALL, "srt"))))
check("传 ass:ssa 算同一家族", ids(U.clobbered(ALL, "ass")) == [4])
check("格式带点、大小写不敏感", ids(U.clobbered(ALL, ".SRT")) == [2, 3])
check("keep 的那条(对轴要替换的旧流)不算误伤", ids(U.clobbered(ALL, "srt", keep=2)) == [3])
check("keep 传字符串 id 也认", ids(U.clobbered(ALL, "srt", keep="2")) == [3])
check("没有同格式外挂:空", U.clobbered([EMB, UP_SSA], "srt") == [])
d = U.describe([UP_SRT, MNT_SRT])
check("describe 写清 id、语言、来源", "id=2 语言未知 上传" in d and "id=3 zho 挂载" in d, d)

print("\n%s" % ("全部通过" if not fails else "失败 %d 项: %s" % (len(fails), "、".join(fails))))
sys.exit(1 if fails else 0)
