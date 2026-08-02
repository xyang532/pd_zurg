# -*- coding: utf-8 -*-
"""
rd_autoheal 健康判定单测。两条最要命的性质用断言锁死:
  · 连续被限流**不得**把健康文件判死(否则会自动删掉能用的片源)
  · 503 要等够天数才补替代,原件一恢复就要撤掉补的
"""
import sys, os, time

os.chdir("/")
sys.path.insert(0, "/plex_debrid")
sys.argv = ["rd_autoheal.py", "--scan-only"]

import importlib.util
# 优先用仓库里那份;容器里跑时退回 docker cp 进去的 /tmp/ah.py
HERE = os.path.dirname(os.path.abspath(__file__))
for cand in (os.path.join(HERE, "..", "tools", "autoheal", "rd_autoheal.py"),
             "/plex_debrid/../tools/autoheal/rd_autoheal.py", "/tmp/ah.py"):
    if os.path.exists(cand):
        TARGET = cand
        break
else:
    raise SystemExit("找不到 rd_autoheal.py")
print("被测模块: %s" % TARGET)
spec = importlib.util.spec_from_file_location("ah", TARGET)
ah = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ah)

FAKE_TOR = [{"id": "T1", "filename": "Some.Movie.mkv", "links": ["L0"]}]
ah.rd = lambda *a, **k: (200, {"files": [{"path": "/Some.Movie.mkv", "selected": 1}]})
LOG = []
ah.log = lambda kind, msg: LOG.append((kind, msg))
ah.args.recheck_days = 0.0
ah.args.degraded_days = 3.0

fails = []


def case(name, codes, expect_health, expect_dead):
    state = {"links": {}}
    found = []
    for c in codes:
        ah.unrestrict_code = lambda ln, _c=c: (_c, "")
        found, _deg, _res = ah.scan(FAKE_TOR, state)
    h = state["links"]["T1#0"].get("health")
    ok = (h == expect_health) and (bool(found) == expect_dead)
    if not ok:
        fails.append(name)
    print("  %-4s %-32s 码=%-12s -> health=%r dead=%s"
          % ("OK" if ok else "FAIL", name, codes, h, bool(found)))


print("健康判定:")
case("一直 200", [200, 200], "ok", False)
case("451 立刻判死", [451], "dead", True)
case("连续两次 429 不得判死", [429, 429], None, False)
case("429 后恢复 200", [429, 200], "ok", False)
case("200 之后 451", [200, 451], "dead", True)
case("连续两次 404 才判死", [404, 404], "dead", True)
case("单次 404 只算可疑", [404], "suspect", False)
case("404 后 200 恢复", [404, 200], "ok", False)
case("451 之后被限流仍算死", [451, 429], "dead", True)

print("\n长期 503 -> 先补替代 -> 原件恢复后撤除:")


def case503(name, seq, want_degraded, want_restore):
    state = {"links": {}}
    deg, res = [], []
    for code, age_days in seq:
        ah.unrestrict_code = lambda ln, _c=code: (_c, "")
        _found, deg, res = ah.scan(FAKE_TOR, state)
        rec = state["links"].get("T1#0", {})
        if rec.get("unavail_since") and age_days:
            # 把"首次不可用"时间往前拨,模拟已经持续了这么多天
            rec["unavail_since"] = int(time.time() - age_days * 86400)
    ok = (bool(deg) == want_degraded) and (bool(res) == want_restore)
    if not ok:
        fails.append(name)
    print("  %-4s %-32s -> degraded=%-5s restore=%-5s (期望 %s/%s)"
          % ("OK" if ok else "FAIL", name, bool(deg), bool(res), want_degraded, want_restore))


case503("503 才 1 天,不动它", [(503, 1)], False, False)
case503("503 满 4 天,补替代", [(503, 4), (503, 4)], True, False)
case503("503 后恢复 200,撤替代", [(503, 4), (200, 0)], False, True)
case503("一直 200 不触发", [(200, 0), (200, 0)], False, False)

print("\n永久 vs 可恢复(决定旧片源删不删):")
for name, code, want_perm in (("451 = 永久,允许删旧", 451, True),
                              ("404 判死但可恢复,不许删旧", 404, False)):
    state = {"links": {}}
    ah.unrestrict_code = lambda ln, _c=code: (_c, "")
    ah.scan(FAKE_TOR, state)
    ah.scan(FAKE_TOR, state)
    got = bool(state["links"]["T1#0"].get("permanent"))
    ok = got == want_perm
    if not ok:
        fails.append(name)
    print("  %-4s %-32s permanent=%s (期望 %s)" % ("OK" if ok else "FAIL", name, got, want_perm))

print("\n质量比较(原件恢复后留谁):")
Q = ah.quality
qcases = [
    ("REMUX 胜 BluRay", ("X.1080p.BluRay.REMUX.mkv", 10e9), ("X.1080p.BluRay.x264.mkv", 10e9), True),
    ("BluRay 胜 WEB-DL", ("X.1080p.BluRay.x264.mkv", 5e9), ("X.1080p.WEB-DL.mkv", 5e9), True),
    ("1080 胜 720(即使 720 是 REMUX)", ("X.1080p.WEB-DL.mkv", 3e9), ("X.720p.BluRay.REMUX.mkv", 9e9), True),
    ("同档比体积", ("X.1080p.BluRay.REMUX.mkv", 12e9), ("X.1080p.BluRay.REMUX.mkv", 8e9), True),
    ("完全相同 -> 平手(留原件)", ("X.1080p.BluRay.REMUX.mkv", 9e9), ("X.1080p.BluRay.REMUX.mkv", 9e9), None),
    ("1080i 不算未知", ("X.1080i.BluRay.REMUX.mkv", 9e9), ("X.720p.BluRay.REMUX.mkv", 20e9), True),
]
for name, a, b, a_wins in qcases:
    qa, qb = Q(*a), Q(*b)
    got = None if qa == qb else (qa > qb)
    ok = got == a_wins
    if not ok:
        fails.append(name)
    print("  %-4s %-32s %s vs %s -> 前者胜=%s" % ("OK" if ok else "FAIL", name, qa, qb, got))

print("\n恢复后留谁(替代要明显更好才留它):")
CB = ah.clearly_better
cbcases = [
    ("替代只大 3% -> 回原件", (1080, 3, 10.71e9), (1080, 3, 10.38e9), False),
    ("替代大 20% -> 留替代", (1080, 3, 12.5e9), (1080, 3, 10.0e9), True),
    ("替代分辨率更高 -> 留替代", (2160, 1, 8e9), (1080, 3, 30e9), True),
    ("替代等级更高(同分辨率)", (1080, 3, 9e9), (1080, 2, 9e9), True),
    ("替代等级更低 -> 回原件", (1080, 1, 40e9), (1080, 3, 9e9), False),
    ("完全相同 -> 回原件", (1080, 3, 9e9), (1080, 3, 9e9), False),
]
for name, newq, oldq, want in cbcases:
    got = CB(newq, oldq)
    ok = got == want
    if not ok:
        fails.append(name)
    print("  %-4s %-32s -> 留替代=%s (期望 %s)" % ("OK" if ok else "FAIL", name, got, want))

print("\n增量复查:")
ah.args.recheck_days = 7.0
state = {"links": {"T1#0": {"health": "ok", "strikes": 0, "code": 200, "ts": time.time()}}}
calls = []
ah.unrestrict_code = lambda ln: (calls.append(1), (200, ""))[1]
ah.scan(FAKE_TOR, state)
ok = len(calls) == 0
print("  %-4s 近期健康的链接本轮不再探测(实际 %d 次)" % ("OK" if ok else "FAIL", len(calls)))
if not ok:
    fails.append("增量复查")

state["links"]["T1#0"]["ts"] = time.time() - 8 * 86400
calls = []
ah.scan(FAKE_TOR, state)
ok = len(calls) == 1
print("  %-4s 超过 recheck-days 会重测(实际 %d 次)" % ("OK" if ok else "FAIL", len(calls)))
if not ok:
    fails.append("过期重测")

print("\n常量: MIN_SIZE_RATIO=%.2f CONFIRM_RUNS=%d degraded_days=%.1f"
      % (ah.MIN_SIZE_RATIO, ah.CONFIRM_RUNS, ah.args.degraded_days))
print("\n%s" % ("全部通过(29 条)" if not fails else "失败: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
