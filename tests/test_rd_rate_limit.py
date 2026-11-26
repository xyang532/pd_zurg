# -*- coding: utf-8 -*-
"""
RD 限流(429)单测。锁死的性质:
  · 两次 addMagnet 至少隔 add_interval 秒
  · 429 时等 add_backoff 再试**同一个**候选,而不是跳到下一个
  · 重试用完仍 429:给条目打 rate_limited、本轮不再试剩下的候选
  · 带 rate_limited 的那轮 watch() 不计失败、不进冷却;下一轮照常计数
  · debrid_download 只认本次调用里打的标记来停循环,但保留之前的标记给 watch()
  · (11-24)加种间隔 ≥ 20 秒,且跨进程:节拍文件里别的进程(autoheal)的时间也算;文件坏了退回进程内并吭声
  · (11-24)名字命中 RD 拒收名单的候选不加;名单外的 451 照常换下一个,不当限流

背景:10-29 debug 6 分钟 293 次 addMagnet,233 次 429;被 429 的候选 RD 根本没判,条目却被记成"没找到"进 12h 冷却。
跑法(容器里): docker cp 仓库的 plex_debrid 和 tests 到同一目录,/venv/bin/python3 tests/test_rd_rate_limit.py
"""
import os, sys, json, tempfile, time
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
for cand in (os.path.join(HERE, "..", "plex_debrid"), "/plex_debrid"):
    if os.path.exists(os.path.join(cand, "debrid", "services", "realdebrid.py")):
        PD = os.path.abspath(cand)
        break
else:
    raise SystemExit("找不到 plex_debrid/debrid/services/realdebrid.py")
print("被测代码: %s" % PD)
sys.path.insert(0, PD)
os.chdir("/")  # trakt 模块导入时从当前目录的 .env 读凭据,和正式进程一样在 / 下跑

import ui  # 照 main.py 的顺序先导 ui;直接导 content 会撞上 plex_debrid 模块间的循环 import
from content import classes
from debrid.services import realdebrid as rd
import rd_policy
assert os.path.dirname(os.path.abspath(rd_policy.__file__)) == PD, "rd_policy 不是被测目录里那份: %s" % rd_policy.__file__

# 假时钟:sleep 只推进时间、记下睡了多久
CLOCK = [1_800_000_000.0]
SLEEPS = []
time.time = lambda: CLOCK[0]
time.sleep = lambda s: (SLEEPS.append(round(s, 3)), CLOCK.__setitem__(0, CLOCK[0] + s))

TMP = tempfile.mkdtemp(prefix="rd_rate_limit_test_")
rd_policy.STAMP_PATH = os.path.join(TMP, "rd_last_add")  # 别碰正式的 /config/rd_last_add
classes.retry_after.path = os.path.join(TMP, "retry_after.json")
classes.retry_after.entries = None

fails = []


def expect(cond, msg):
    if not cond:
        fails.append(msg)


R429 = lambda: SimpleNamespace(error="too_many_requests", error_code=34)
R451 = lambda: SimpleNamespace(error="infringing_file", error_code=35)
POSTS = []


def fake_post(answers):
    it = iter(answers)
    def post(url, data):
        POSTS.append(data.get("magnet"))
        return next(it)()
    return post


def reset():
    POSTS.clear(); SLEEPS.clear(); rd_policy._last[0] = 0.0
    if os.path.isfile(rd_policy.STAMP_PATH):
        os.remove(rd_policy.STAMP_PATH)


# 1. 429 两次后 RD 给出真判定(451):三次都发的是同一个候选,中间按 add_backoff 等
reset(); rd.post = fake_post([R429, R429, R451])
r = rd.add_magnet("magnet:A", "A")
expect(getattr(r, "error", None) == "infringing_file", "429 之后没有拿到真判定: %r" % r)
expect(POSTS == ["magnet:A"] * 3, "重试的不是同一个候选: %r" % POSTS)
expect(15 in SLEEPS and 30 in SLEEPS, "没有按 add_backoff 退避: %r" % SLEEPS)

# 2. 连续两次添加:第二次之前要补足 add_interval
reset(); rd.post = fake_post([R451, R451])
rd.add_magnet("magnet:A"); t0 = CLOCK[0]; rd.add_magnet("magnet:B")
expect(abs(CLOCK[0] - t0 - rd.add_interval) < 1e-6, "两次添加间隔不是 %ss: %.3f" % (rd.add_interval, CLOCK[0] - t0))

# 3. 一直 429:download() 打标记、返回 False,第二个候选碰都不碰
reset(); rd.post = fake_post([R429] * (1 + len(rd.add_backoff)) + [R451])
el = SimpleNamespace(Releases=[SimpleNamespace(title="A", download=["magnet:A"]), SimpleNamespace(title="B", download=["magnet:B"])],
                     deviation=lambda *a, **k: ".*")
ok = rd.download(el)
expect(ok is False, "一直 429 时 download 没返回 False")
expect(getattr(el, "rate_limited", False), "一直 429 时没打 rate_limited")
expect(POSTS == ["magnet:A"] * (1 + len(rd.add_backoff)), "一直 429 时调用次数/对象不对: %r" % POSTS)


# 4. watch():被限流的那轮不计失败、不进冷却;标记用完即清,下一轮照常计数
def episode():
    m = classes.media(SimpleNamespace(type="episode", grandparentTitle="Stranger Things", grandparentYear=2016,
                                      grandparentEID=["imdb://tt4574334"], parentIndex=4, index=8))
    m.versions = lambda *a, **k: [SimpleNamespace(name="1080p SDR", rules=[], triggers=[["retries", "<=", "48"]])]
    return m


ep = episode(); ep.rate_limited = True
ep.watch()
expect(ep not in classes.media.ignore_queue, "被限流的一轮仍计了一次失败")
expect(not classes.retry_after.known(ep), "被限流的一轮仍进了冷却记录")
expect(ep.rate_limited is False, "watch() 后没清掉 rate_limited")
ep.watch()
expect(ep in classes.media.ignore_queue, "没有限流的一轮没有照常计数")
classes.media.ignore_queue.clear()

# 已冷却过的条目(retry_after 里有记录)被限流时也不能再冷却 12h
ep2 = episode(); classes.retry_after.defer(ep2); before = dict(classes.retry_after.load()[ep2.query().lower()])
ep2.rate_limited = True; ep2.watch()
expect(classes.retry_after.load()[ep2.query().lower()] == before, "已冷却条目被限流时又被延后了")

# 5. debrid_download:本次调用里被限流就停;之前遗留的标记不截断循环,但留给 watch()
classes.debrid.check = lambda *a, **k: None
classes.releases.sort = lambda *a, **k: None
classes.releases.print_releases = lambda *a, **k: None
CALLS = []


def media_with_releases(n):
    m = episode(); m.bitrate = lambda: None
    m.Releases = [SimpleNamespace(title="R%d" % i, cached=["RD"]) for i in range(n)]
    return m


def limited_download(element, stream=True, force=False):
    CALLS.append(element.Releases[0].title); element.rate_limited = True; return False


def plain_fail(element, stream=True, force=False):
    CALLS.append(element.Releases[0].title); return False


CALLS.clear(); classes.debrid.download = limited_download
m = media_with_releases(3); res = m.debrid_download()
expect(CALLS == ["R0"], "被限流后仍继续试后面的候选: %r" % CALLS)
expect(res == (False, True) and m.rate_limited, "被限流后返回值/标记不对: %r %r" % (res, m.rate_limited))

CALLS.clear(); classes.debrid.download = plain_fail
m = media_with_releases(3); m.rate_limited = True; m.debrid_download()
expect(CALLS == ["R0", "R1", "R2"], "遗留的旧标记把候选循环截断了: %r" % CALLS)
expect(m.rate_limited, "遗留的标记在 debrid_download 后丢了(watch 就会误计失败)")

# 6. RD 拒收名单(11-24 事故:14 个带 WEB-DL/WEBRip 的候选全 451)
expect(rd.add_interval >= 20, "加种间隔低于 RD 的 20 秒门槛: %s" % rd.add_interval)
for title, want in [
    ("You.Can.See.Everything.2026.REPACK2.1080p.AMZN.WEB-DL.DDP5.1.Atmos.H.264-CHORTLE", "WEB-DL"),
    ("You.Can.See.Everything.2026.1080p.AMZN.WEB.DL.DDP5.1.H.264-CHORTLE {imdb-tt45368240}", None),  # 点号放行(实测 201)
    ("Some.Movie.2020.1080p.web-dl.x264", None),            # WEB-DL 只在大小写完全一致时拒
    ("Some.Movie.2020.1080p.webrip.x265", "webrip"),        # WEBRip 不分大小写
    ("Some.Movie.2020.1080p.BluRay-x264", None),            # 分隔符换掉就放行
    ("Some.Movie.2020.1080p.BluRay.x264-GRP", "BluRay.x264"),
    ("Some.Movie.2020.1080p.BluRay.DTS-HD.MA.5.1", "p.BluRay.DTS"),
    ("Big Buck Bunny", None), ("", None), (None, None),
]:
    got = rd_policy.refused_name(title)
    expect(got == want, "refused_name(%r) = %r,应为 %r" % (title, got, want))

# 7. download():名字被拒的候选不加(不占加种间隔),直接试下一个;名单外的 451 照常换下一个
reset(); rd.post = fake_post([R451])
el = SimpleNamespace(Releases=[SimpleNamespace(title="X.2026.1080p.AMZN.WEB-DL.H.264-G", download=["magnet:refused"]),
                               SimpleNamespace(title="X.2026.1080p.AMZN.WEB.DL.H.264-G", download=["magnet:ok"])],
                     deviation=lambda *a, **k: ".*")
ok = rd.download(el)
expect(POSTS == ["magnet:ok"], "名字被 RD 拒收的候选仍被加了,或下一个没试: %r" % POSTS)
expect(ok is False and not getattr(el, "rate_limited", False), "名单外 451 被当成限流了")

# 8. 跨进程节拍:别的进程 5 秒前刚加过(节拍文件里的时间),这次要再等 16 秒,并把自己的时间写回去
reset()
with open(rd_policy.STAMP_PATH, "w") as fh:
    fh.write("%.3f" % (CLOCK[0] - 5))
slept = rd_policy.wait_turn()
expect(abs(slept - (rd_policy.ADD_INTERVAL - 5)) < 1e-6, "没按节拍文件里别的进程的时间等: 睡了 %.3f" % slept)
with open(rd_policy.STAMP_PATH) as fh:
    expect(abs(float(fh.read()) - CLOCK[0]) < 1e-3, "加完没把自己的时间写回节拍文件")

# 9. 节拍文件用不了:退回进程内节拍(仍然隔够),并在 stderr 说一次
import io, contextlib
reset(); saved = rd_policy.STAMP_PATH; rd_policy.STAMP_PATH = TMP  # 是个目录,打不开成文件
err = io.StringIO()
with contextlib.redirect_stderr(err):
    rd_policy.wait_turn(); t0 = CLOCK[0]; rd_policy.wait_turn()
rd_policy.STAMP_PATH = saved
expect(abs(CLOCK[0] - t0 - rd_policy.ADD_INTERVAL) < 1e-6, "节拍文件坏了以后进程内也不节流了")
expect("节拍文件" in err.getvalue(), "节拍文件用不了却没吭声")

if fails:
    print("FAIL %d 条:" % len(fails))
    for f in fails:
        print("  " + f)
    sys.exit(1)
print("OK RD 限流全部通过")
