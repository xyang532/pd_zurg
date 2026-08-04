# -*- coding: utf-8 -*-
"""align_subs 的时间轴数学单测。

这套逻辑会**改写用户的字幕文件**,所以两类性质必须锁死:
  · 能把已知偏移原样量回来(含条数不等、抖动、缺条这些真实情况);
  · **量不准时必须报不出**,绝不能拿随机撞上的"峰值"去改写一条本来正确的字幕。
"""
import io, os, random, sys
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
for cand in (os.path.join(HERE, "..", "tools", "subs", "align_subs.py"),
             os.path.join(HERE, "align_subs.py"), "/tmp/align_subs.py"):
    if os.path.exists(cand):
        TARGET = cand
        break
else:
    raise SystemExit("找不到 align_subs.py")
print("被测模块: %s" % TARGET)
spec = importlib.util.spec_from_file_location("al", TARGET)
al = importlib.util.module_from_spec(spec)
spec.loader.exec_module(al)

fails = []


def check(name, ok, extra=""):
    print("  %-4s %s%s" % ("OK" if ok else "FAIL", name, ("  " + extra) if extra else ""))
    if not ok:
        fails.append(name)


def dialogue(n, start=60.0, seed=1):
    """造一串像对白的 cue 起始时间:间隔不均匀,才测得出算法不是靠等间距蒙对的。"""
    rnd = random.Random(seed)
    t, out = start, []
    for _ in range(n):
        t += rnd.uniform(1.0, 6.0)
        out.append(round(t, 3))
    return out


def sup_bytes(times):
    """造一段 PGS:每个时刻一个"显示"PCS(对象数=1),再跟一个"擦除"PCS(对象数=0)。"""
    out = b""
    for t in times:
        for nobj in (1, 0):
            payload = bytes(10) + bytes([nobj])
            pts = int(t * 90000)
            out += (b"PG" + pts.to_bytes(4, "big") + (0).to_bytes(4, "big")
                    + bytes([0x16]) + len(payload).to_bytes(2, "big") + payload)
    return out


print("\n[1] 时间码解析")
SRT = ("1\n00:00:13,914 --> 00:00:17,190\n\xe4\xbd\xa0\xe5\xa5\xbd\n\n"
       "2\n01:02:03,004 --> 01:02:05,000\nhi\n\n")
check("SRT 毫秒", al.cue_starts(SRT) == [13.914, 3723.004], str(al.cue_starts(SRT)))
ASS = ("[Events]\nFormat: Layer, Start, End, Style\n"
       "Dialogue: 0,0:00:13.91,0:00:17.19,Default,,0,0,0,,\xe4\xbd\xa0\xe5\xa5\xbd\n"
       "Dialogue: 0,0:01:00.05,0:01:02.00,Default,,0,0,0,,hi\n")
check("ASS 百分秒补成毫秒", al.cue_starts(ASS) == [13.91, 60.05], str(al.cue_starts(ASS)))
check("非时间码行不误抓", al.cue_starts("Title: 12:34:56,789 something") == [])

print("\n[2] PGS 段解析")
t = [10.0, 12.5, 20.25]
check("只取显示事件,不取擦除", al.parse_sup(sup_bytes(t)) == t, str(al.parse_sup(sup_bytes(t))))
check("空输入不炸", al.parse_sup(b"") == [])
_full = sup_bytes(t)
_cut = al.parse_sup(_full[:len(_full) * 2 // 3 + 4])   # 砍在第三个"显示"段中间
check("半截的段丢掉而不是崩", _cut == t[:2], str(_cut))
check("非 PGS 数据不炸", al.parse_sup(b"not a sup file at all") == [])

print("\n[3] 求偏移 —— 能量回来")
base = dialogue(60)
for delta in (0.0, 0.35, -0.72, 2.5, -12.0, 25.0):
    ref = [x + delta for x in base]
    r = al.best_shift(base, ref)
    ok = r is not None and abs(r[0] - delta) < 0.02
    check("偏移 %+.2fs 量回 %s" % (delta, ("%+.3f" % r[0]) if r else "None"), ok)

print("\n[4] 求偏移 —— 真实世界的脏数据")
rnd = random.Random(7)
delta = 1.8
# 中文字幕常把两句英文并成一条:条数不等、起点略早
merged = [x for i, x in enumerate(base) if i % 3 != 1]
ref = [x + delta + rnd.uniform(-0.05, 0.05) for x in base]
r = al.best_shift(merged, ref)
check("条数不等(40 vs 60)+ 抖动", r is not None and abs(r[0] - delta) < 0.08,
      ("%+.3f" % r[0]) if r else "None")

# 参照轨少了一半的条(forced 之外也常见:SDH 有音效条,普通轨没有)
half = ref[::2]
r = al.best_shift(base, half)
check("参照条数只有一半", r is not None and abs(r[0] - delta) < 0.08,
      ("%+.3f" % r[0]) if r else "None")

print("\n[5] 求偏移 —— 量不准时必须报不出")
rnd = random.Random(11)
noise = sorted(rnd.uniform(60, 400) for _ in range(60))
r = al.best_shift(base, noise)
check("两串毫不相干 -> None 或低一致度", r is None or r[2] < al.MIN_RATIO,
      "None" if r is None else "ratio=%.2f" % r[2])
check("条数不足 -> None", al.best_shift(base[:3], ref) is None)
check("空输入 -> None", al.best_shift([], ref) is None)
r = al.best_shift(base, [x + 90.0 for x in base])
check("偏移超出搜索范围 -> None", r is None, "None" if r is None else "%+.1f" % r[0])

print("\n[6] 帧率漂移判定")
a, b = al.fit_drift(600.0, 1.0, 3600.0, 1.0)
check("两窗口一致 -> 斜率 1", abs(a - 1.0) < 1e-9 and abs(b - 1.0) < 1e-9)
# PAL 提速:25/23.976,一小时差 ~154 秒
a, b = al.fit_drift(600.0, 600.0 * (25.0 / 23.976 - 1), 3600.0, 3600.0 * (25.0 / 23.976 - 1))
check("PAL 比被认出", al.known_fps_ratio(a) is not None, "a=%.6f" % a)
check("任意漂移不冒充帧率", al.known_fps_ratio(1.02) is None)

print("\n[7] 改写时间码")
out = al.shift_text(SRT, 1.0, 2.0)
check("SRT 平移 +2s", al.cue_starts(out) == [15.914, 3725.004], str(al.cue_starts(out)))
check("SRT 保留逗号与三位", "00:00:15,914 --> 00:00:19,190" in out, out.splitlines()[1])
out = al.shift_text(ASS, 1.0, 2.0)
check("ASS 平移后仍是两位百分秒", "0:00:15.91," in out and "0:01:02.05," in out,
      [l for l in out.splitlines() if l.startswith("Dialogue")][0][:40])
check("负数被夹到 0", al.cue_starts(al.shift_text(SRT, 1.0, -100.0))[0] == 0.0)
# 进位:59.999 + 0.002 必须变成下一分钟的 00.001,不能写成 :60.001
carry = al.shift_text("1\n00:00:59,999 --> 00:01:02,000\nx\n", 1.0, 0.002)
check("秒进位正确", "00:01:00,001" in carry, carry.splitlines()[1])
scaled = al.shift_text(SRT, 25.0 / 24.0, 0.0)
check("带缩放改写", abs(al.cue_starts(scaled)[0] - 13.914 * 25.0 / 24.0) < 0.001,
      str(al.cue_starts(scaled)[0]))

print("\n[8] 窗口选取")
cues = dialogue(400, start=60.0)
w = al.pick_windows(cues, 7000.0)
check("长片给三个窗口", len(w) == 3, str(w))
check("窗口互不重叠", all(w[i + 1] - w[i] >= al.WIN_SEC for i in range(len(w) - 1)), str(w))
check("条数太少不给窗口", al.pick_windows(cues[:5], 7000.0) == [])
w2 = al.pick_windows(dialogue(400, start=10.0), 400.0)
check("窗口不越过片尾", all(x + al.WIN_SEC <= 400.0 + 1e-6 for x in w2), str(w2))
check("短片挤不下就少给几个", len(w2) < 3, str(w2))

print("\n[8b] 漂移判据看的是整片累计,不是两窗口差值")
# 两窗口只差 0.17 秒,但窗口间距只有 300 秒时斜率不小 —— 关键看乘上片长是多少
a1, _ = al.fit_drift(300.0, 20.9, 600.0, 21.07)
check("小差值 + 短基线 -> 累计仍可能超标", abs(a1 - 1.0) * 6000 > al.DRIFT_TOL,
      "累计 %.1fs" % (abs(a1 - 1.0) * 6000))
a2, _ = al.fit_drift(900.0, 20.9, 5400.0, 21.07)
check("小差值 + 长基线 -> 判为纯平移", abs(a2 - 1.0) * 6000 <= al.DRIFT_TOL,
      "累计 %.2fs" % (abs(a2 - 1.0) * 6000))
check("噪声级斜率不再冒充帧率比", al.known_fps_ratio(a2) is None, "a=%.6f" % a2)
check("真 PAL 斜率仍认得出", al.known_fps_ratio(25.0 / 23.976) is not None)

print("\n[8c] 窗口够不够硬 —— 虚的窗口不许决定'有没有漂移'")
check("票多且一致度高 -> 硬", al.solid((1.0, 40, 0.60)))
check("票太少 -> 虚", not al.solid((1.0, 14, 0.60)), "14 票")
check("一致度太低 -> 虚", not al.solid((1.0, 40, 0.39)), "39%")
check("None -> 虚", not al.solid(None))
# 《X圣治》实况:窗口一 23票/62% 硬,窗口二 14票/39% 虚 -> 只能按整体平移,不该判 MISMATCH
w_firm, w_weak = (1.0, 23, 0.62), (1.0, 14, 0.39)
check("X圣治 那组:只有一个硬窗口", len([w for w in (w_firm, w_weak) if al.solid(w)]) == 1)

check("SRT 小时位宽保持两位", al.shift_text("00:00:13,914 --> x", 1.0, 0.0)
      .startswith("00:00:13,914"), al.shift_text("00:00:13,914 --> x", 1.0, 0.0))
check("ASS 小时位宽保持一位", al.shift_text("0:00:13.91,", 1.0, 0.0).startswith("0:00:13.91"),
      al.shift_text("0:00:13.91,", 1.0, 0.0))


def srt_ts(x):
    return "%02d:%02d:%02d,%03d" % (int(x) // 3600, (int(x) // 60) % 60, int(x) % 60,
                                    round((x - int(x)) * 1000))


print("\n[9] 端到端:造一条错位字幕,量出来再改回去")
truth = dialogue(120, start=120.0, seed=3)
srt = "".join("%d\n%s --> %s\nline\n\n" % (i + 1, srt_ts(x), srt_ts(x + 2))
              for i, x in enumerate([c - 3.4 for c in truth]))
got = al.best_shift(al.cue_starts(srt), truth)
check("量出 +3.4s", got is not None and abs(got[0] - 3.4) < 0.02,
      ("%+.3f" % got[0]) if got else "None")
fixed = al.shift_text(srt, 1.0, got[0])
again = al.best_shift(al.cue_starts(fixed), truth)
check("改写后残余偏移 ~0", again is not None and abs(again[0]) < 0.02,
      ("%+.4f" % again[0]) if again else "None")

print("\n常量: WIN=%.0fs MAX_SHIFT=%.0fs BIN=%.0fms MIN_PAIRS=%d MIN_RATIO=%.2f "
      "OK_SHIFT=%.2fs DRIFT_TOL=%.2fs FPS_TOL=%.4f DRIFT_MIN=%d/%.2f WEAK_MIN=%.1fs"
      % (al.WIN_SEC, al.MAX_SHIFT, al.BIN_SEC * 1000, al.MIN_PAIRS, al.MIN_RATIO,
         al.OK_SHIFT, al.DRIFT_TOL, al.FPS_TOL, al.DRIFT_MIN_PAIRS, al.DRIFT_MIN_RATIO,
         al.MIN_WEAK_SHIFT))
print("\n%s" % ("全部通过" if not fails else "失败: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
