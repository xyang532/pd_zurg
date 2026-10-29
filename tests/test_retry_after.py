# -*- coding: utf-8 -*-
"""
连败降频(retry_after)单测。锁死的性质:
  · 连败用完重试预算后**不得**写进 ignored.txt(那是永久放弃),而是等 retry_after.hours 再试
  · 等待时间存盘,模拟重启(清空内存)后仍在等
  · 降过一次频的条目之后每轮只试一次,失败立刻再等,不再重走 48 轮
  · 下载成功就清掉记录;记录文件损坏时挪到一边、不覆盖

跑法(容器里): docker cp 仓库的 plex_debrid 和 tests 到同一目录,/venv/bin/python3 tests/test_retry_after.py
"""
import os, sys, json, glob, tempfile, time
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

import ui  # 照 main.py 的顺序先导 ui;直接导 content 会撞上 plex_debrid 模块间的循环 import
from content import classes

TMP = tempfile.mkdtemp(prefix="retry_after_test_")
classes.retry_after.path = os.path.join(TMP, "retry_after.json")
classes.retry_after.entries = None
NOW = [1_800_000_000.0]
time.time = lambda: NOW[0]
IGNORED = []
classes.ignore.add = lambda self: IGNORED.append(self.query())

fails = []


def expect(cond, msg):
    if not cond:
        fails.append(msg)


def episode():
    m = classes.media(SimpleNamespace(type="episode", grandparentTitle="Friends", grandparentYear=1994,
                                      grandparentEID=["imdb://tt0108778"], parentIndex=7, index=1))
    m.versions = lambda *a, **k: [SimpleNamespace(name="1080p SDR", triggers=[["retries", "<=", "48"]])]
    return m


ep = episode()
for i in range(48):
    ep.watch()
expect(not ep.watched(), "48 轮内就被搁置了")
ep.watch()   # 第 49 次失败 = 旧逻辑写 ignored.txt 的那一刻
expect(IGNORED == [], "仍然写进了忽略清单: %r" % IGNORED)
expect(ep.watched(), "用完重试预算后没有进入等待")
expect(ep not in classes.media.ignore_queue, "进入等待后还留在内存计数队列里")
entry = json.load(open(classes.retry_after.path))[ep.query().lower()]
expect(entry["rounds"] == 1 and abs(entry["next"] - (NOW[0] + 12 * 3600)) < 1, "等待时间不对: %r" % entry)

classes.retry_after.entries = None            # 模拟容器重启
expect(episode().watched(), "重启后等待丢了")

NOW[0] += 12 * 3600 + 1
ep2 = episode()
expect(not ep2.watched(), "12 小时后没有放行重试")
ep2.watch()                                   # 放行后又失败一次
expect(ep2.watched(), "降过频的条目失败一次后没有立刻再等")
expect(ep2 not in classes.media.ignore_queue, "降过频的条目又进了 48 轮计数")
expect(json.load(open(classes.retry_after.path))[ep2.query().lower()]["rounds"] == 2, "第二次等待的轮数不对")

ep2.version = SimpleNamespace(name="1080p SDR")
ep2.downloaded()
expect(not classes.retry_after.known(ep2), "下载成功后记录没清掉")
expect(ep2.query().lower() not in json.load(open(classes.retry_after.path)), "下载成功后文件里还有记录")

with open(classes.retry_after.path, "w") as f:
    f.write("{坏的")
classes.retry_after.entries = None
expect(not episode().watched(), "记录文件损坏时应当按空表处理")
expect(len(glob.glob(classes.retry_after.path + ".unreadable-*")) == 1, "损坏的记录文件没有被挪到一边保留")

if fails:
    print("FAIL %d 条:" % len(fails))
    for f in fails:
        print("  " + f)
    sys.exit(1)
print("OK 连败降频全部通过")
