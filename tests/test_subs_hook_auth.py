# -*- coding: utf-8 -*-
"""subs_hook 令牌失效处理单测,对着一个只认新令牌的假 Plex 跑。

**存在理由是 10-31 的整队堵死**:服务器令牌 10-28 作废换新,settings.json 跟着改了,
常驻的监听器却一直拿启动时缓存的旧令牌。每个请求 401,而 px() 把 401 吞成"没数据",
ready() 报「Plex 还没分析完」,每部空等 20 分钟后跳过 —— 用户打分 40 分钟没反应。
所以这里断言:令牌换了就自己重读;重读后仍被拒要立刻、如实地报,不许再等。
"""
import io, json, os, sys, tempfile, threading, time
import importlib.util
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

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


GOOD = "new-token"
STATE = {"by_tok": {}, "status": None}
MOVIE = {"MediaContainer": {"Metadata": [{
    "title": "汉娜姐妹", "duration": 6415000,
    "Media": [{"Part": [{"file": "/rclone/x.mkv", "Stream": []}]}]}]}}


class FakePlex(BaseHTTPRequestHandler):
    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        tok = (q.get("X-Plex-Token") or [""])[0]
        STATE["by_tok"][tok] = STATE["by_tok"].get(tok, 0) + 1
        if STATE["status"]:
            code = STATE["status"]
        else:
            code = 200 if tok == GOOD else 401
        body = json.dumps(MOVIE).encode() if code == 200 else b""
        self.send_response(code)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), FakePlex)
threading.Thread(target=srv.serve_forever, daemon=True).start()

TMP = tempfile.mkdtemp(prefix="subs_hook_auth_")
sh.CFG_PATH = os.path.join(TMP, "settings.json")
sh.LOG_PATH = os.path.join(TMP, "subs.log")
sh.PLEX_HOST_URL = "http://127.0.0.1:%d" % srv.server_port
sh.READY_TRIES, sh.READY_WAIT = 3, 0.01
os.environ.pop("PLEX_TOKEN", None)
ran = []
sh.in_container = lambda script, rk, extra=(): ran.append(script)
sh.run = lambda cmd, tag: ran.append(tag)


def set_token(tok):
    with io.open(sh.CFG_PATH, "w", encoding="utf-8") as f:
        f.write(json.dumps({"Plex users": [["EInsipid", tok]]}))


def logtext():
    try:
        return io.open(sh.LOG_PATH, encoding="utf-8").read()
    except IOError:
        return ""


def reset():
    STATE["by_tok"].clear()
    STATE["status"] = None
    del ran[:]
    if os.path.exists(sh.LOG_PATH):
        os.remove(sh.LOG_PATH)


print("一、令牌换了:缓存着旧的,遇 401 重读 settings.json 后成功")
reset()
set_token("old-token")
sh._cfg[0] = None
sh.cfg()                        # 服务启动后先用过一次 —— 缓存住旧令牌
set_token(GOOD)                 # 之后 settings.json 换成了新令牌(10-28 的真实顺序)
md = sh.ready("4097")
check("ready() 拿到条目", bool(md) and md.get("title") == "汉娜姐妹")
check("旧令牌只被拒 1 次,不是每 30 秒一次地耗满",
      STATE["by_tok"].get("old-token") == 1, str(STATE["by_tok"]))
check("之后的请求直接用新令牌", sh.cfg()[1] == GOOD)

print("二、重读后仍被拒:立刻如实报错,不空等、不跑四步")
reset()
set_token("revoked-token")
sh._cfg[0] = None
t0 = time.time()
sh.handle("4097", recheck=True, why="手动")
took = time.time() - t0
lt = logtext()
check("记一行 AUTH 并说出令牌来源", "AUTH" in lt and "Plex users" in lt and "401" in lt, lt.strip()[-160:])
check("不再误报「还没分析完」", "还没分析完" not in lt)
check("四步一步都没跑", ran == [], str(ran))
check("不重试到底:只发了 2 个请求", sum(STATE["by_tok"].values()) == 2, str(STATE["by_tok"]))
check("立刻返回", took < 2.0, "%.2fs" % took)

print("三、非 401 的读不到(如 500):仍按「还没就绪」等,行为不变")
reset()
set_token(GOOD)
sh._cfg[0] = None
STATE["status"] = 500
sh.handle("4097", why="入库")
lt = logtext()
check("等满后记 SKIP 还没分析完", "还没分析完" in lt and "AUTH" not in lt, lt.strip()[-120:])
check("没跑四步", ran == [], str(ran))

print("四、剧集展开时令牌失效:退回只处理自身,并报错")
reset()
set_token("revoked-token")
sh._cfg[0] = None
out = sh.expand("500", "show")
check("退回 [自身]", out == ["500"], str(out))
check("记 AUTH", "AUTH" in logtext())

srv.shutdown()
print("\n%s" % ("全部通过" if not fails else "失败 %d 项: %s" % (len(fails), "、".join(fails))))
sys.exit(1 if fails else 0)
