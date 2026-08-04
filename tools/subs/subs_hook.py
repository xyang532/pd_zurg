# -*- coding: utf-8 -*-
"""subs_hook —— 接 Plex 的 `library.new` 事件,只给**刚入库的那个条目**配字幕并对轴。

为什么是事件而不是定时扫:用户 2026-08-03 明确 —— 老片不要动,只管新进来的。定时任务
天然是"扫一遍全库",要做到只碰新片就得靠水位线;而 Plex Pass 本来就带 webhook,
入库那一刻直接把 ratingKey 送上门,既没有延迟也不存在"顺手把老片也改了"的可能。

**能干活的最早时刻是"Plex 扫描入库之后",不是 rclone 落盘那一刻**:上传字幕的接口要
ratingKey,判定要片长和片源自带字幕轨,这些都得等 Plex 扫完才有。所以挂在这个事件上,
就是物理上最早的可行点。

四步,和手动那套完全一样,只是每一步都用 --rk 限定到这一个条目:
  fix_subs(Plex 源,不耗外部配额)→ fetch_subs(assrt)→ align_subs(对轴)→ fix_select(补选中)
前两步和第四步在容器里跑(要 /config 和 docker 网络里的 `plex` 主机名),
第三步在宿主机上跑(要宿主机的 ffmpeg 和挂载盘)。
"""
import io, json, os, re, subprocess, sys, threading, time
from email.parser import BytesParser
import urllib.request, urllib.parse, urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    import queue
except ImportError:
    import Queue as queue

CFG_PATH = "/volume1/docker/pd_zurg/config/settings.json"
HOOK_CFG = "/volume1/docker/pd_zurg/config/subs_hook.json"
LOG_PATH = "/volume1/docker/pd_zurg/log/subs.log"
SEEN_PATH = "/volume1/docker/pd_zurg/config/subs_hook_seen.json"
TOOLS = "/volume1/docker/pd_zurg/pd_zurg/tools/subs"
DOCKER = "/usr/local/bin/docker"
PLEX_HOST_URL = "http://127.0.0.1:32400"
PORT = 32499

# Plex 报 library.new 时,媒体分析未必做完 —— 片长可能还是 0,自带字幕轨也可能还没列出来。
# 没有这两样,四步里有三步都判不了,所以先等它就绪再干活。
READY_TRIES, READY_WAIT = 40, 30.0
# 同一个条目短时间内可能收到多次事件(季/剧集/单集各来一发),记下做过的,别重复干
SEEN_KEEP = 4000

_q = queue.Queue()
_cfg = [None]


def log(kind, msg):
    line = "%s %-8s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), kind, msg)
    try:
        with io.open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except IOError:
        pass
    sys.stdout.write(line)
    sys.stdout.flush()


def cfg():
    if _cfg[0] is None:
        c = json.load(io.open(CFG_PATH, encoding="utf-8"))
        _cfg[0] = (PLEX_HOST_URL, os.environ.get("PLEX_TOKEN") or c["Plex users"][0][1])
    return _cfg[0]


def px(path, timeout=60):
    base, tok = cfg()
    u = "%s%s%sX-Plex-Token=%s" % (base, path, "&" if "?" in path else "?", tok)
    r = urllib.request.Request(u, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            b = resp.read()
            return json.loads(b)["MediaContainer"] if b.strip() else {}
    except Exception:
        return {}


def load_seen():
    try:
        return list(json.load(io.open(SEEN_PATH, encoding="utf-8")))
    except Exception:
        return []


def save_seen(seen):
    try:
        with io.open(SEEN_PATH, "w", encoding="utf-8") as f:
            f.write(json.dumps(seen[-SEEN_KEEP:]))
    except IOError:
        pass


def hook_secret():
    """URL 里带一段随机路径当凭据 —— Plex 的 webhook 不支持自定义请求头,没法加认证。
    没有它的话,同一局域网里任何人都能往这个端口投递伪造事件。"""
    try:
        return json.load(io.open(HOOK_CFG, encoding="utf-8"))["secret"]
    except Exception:
        s = os.urandom(16).hex()
        with io.open(HOOK_CFG, "w", encoding="utf-8") as f:
            f.write(json.dumps({"secret": s, "port": PORT}))
        os.chmod(HOOK_CFG, 0o600)
        return s


def expand(rk, typ):
    """剧集的事件可能报在剧/季上,展开成单集 —— 字幕是按集配的。"""
    if typ == "show":
        d = px("/library/metadata/%s/allLeaves" % rk)
    elif typ == "season":
        d = px("/library/metadata/%s/children" % rk)
    else:
        return [str(rk)]
    out = [str(m["ratingKey"]) for m in (d.get("Metadata") or []) if m.get("ratingKey")]
    return out or [str(rk)]


def ready(rk):
    """等 Plex 把这个条目分析完:要有片长,要列得出 Part。"""
    for _ in range(READY_TRIES):
        d = px("/library/metadata/%s" % rk)
        md = (d.get("Metadata") or [{}])[0]
        if (md.get("duration") or 0) > 0:
            for m in md.get("Media") or []:
                if m.get("Part"):
                    return md
        time.sleep(READY_WAIT)
    return None


def run(cmd, tag):
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = p.communicate(timeout=3600)
        if p.returncode != 0:
            log("WARN", "%s 非零退出 %s: %s" % (tag, p.returncode,
                                              out.decode("utf-8", "replace")[-200:].strip()))
    except Exception as e:
        log("WARN", "%s 跑不起来: %s" % (tag, e))


def in_container(script, rk, extra=()):
    src = os.path.join(TOOLS, script)
    run([DOCKER, "cp", src, "pd_zurg:/tmp/%s" % script], "cp %s" % script)
    run([DOCKER, "exec", "pd_zurg", "/venv/bin/python3", "/tmp/%s" % script,
         "--rk", rk, "--apply"] + list(extra), script)


def handle(rk):
    md = ready(rk)
    if md is None:
        log("SKIP", "rk=%s 等了 %d 分钟 Plex 还没分析完,放弃(下次入库事件会再来)"
            % (rk, int(READY_TRIES * READY_WAIT / 60)))
        return
    name = md.get("title") or rk
    if md.get("grandparentTitle"):
        name = "%s S%02dE%02d" % (md["grandparentTitle"],
                                  md.get("parentIndex") or 0, md.get("index") or 0)
    log("NEW", "入库 %s (rk=%s) —— 开始配字幕" % (name[:40], rk))
    in_container("fix_subs.py", rk, ["--mode", "missing"])
    in_container("fetch_subs.py", rk)
    run(["/usr/bin/python3", os.path.join(TOOLS, "align_subs.py"),
         "--rk", rk, "--apply", "--limit", "1"], "align_subs")
    in_container("fix_select.py", rk)
    log("DONE", "%s (rk=%s) 处理完毕" % (name[:40], rk))


def worker():
    seen = load_seen()
    sset = set(seen)
    while True:
        rk = _q.get()
        try:
            if rk in sset:
                log("SKIP", "rk=%s 已经处理过,跳过重复事件" % rk)
                continue
            sset.add(rk)
            seen.append(rk)
            save_seen(seen)
            handle(rk)
        except Exception as e:
            log("WARN", "rk=%s 处理时异常: %s" % (rk, e))
        finally:
            _q.task_done()


# Plex 真发的 payload 分段里多一行 `Content-Type: application/json` —— 手写的测试载荷没有,
# 于是"单测通过、生产解不开"。所以优先按 Content-Type 里的真实 boundary 走标准 multipart
# 解析;正则只当没有 Content-Type 时的兜底,且必须容忍分段里出现任意条额外的头。
PAYLOAD_RE = re.compile(
    rb'name="payload"[^\r\n]*\r?\n(?:[^\r\n]+\r?\n)*\r?\n(.*?)\r?\n--', re.S)


def parse_payload(body, ctype):
    ctype = ctype or ""
    if "application/json" in ctype:
        return json.loads(body.decode("utf-8", "replace"))
    if "multipart/" in ctype:
        try:
            msg = BytesParser().parsebytes(
                b"Content-Type: " + ctype.encode("utf-8", "replace")
                + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
            for part in msg.walk():
                if part.get_param("name", header="content-disposition") == "payload":
                    raw = part.get_payload(decode=True)
                    if raw:
                        return json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            pass
    m = PAYLOAD_RE.search(body)
    if not m:
        return None
    return json.loads(m.group(1).decode("utf-8", "replace"))


class Handler(BaseHTTPRequestHandler):
    secret = ""

    def log_message(self, *a):
        pass                      # 默认会往 stderr 刷访问日志,没用还吵

    def do_POST(self):
        if self.path.strip("/") != self.secret:
            self.send_response(404)
            self.end_headers()
            return
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        # **先回 200 再干活** —— Plex 对 webhook 有超时,慢响应会被当成投递失败
        self.send_response(200)
        self.end_headers()
        try:
            p = parse_payload(body, self.headers.get("Content-Type"))
        except Exception as e:
            log("WARN", "事件解不开(%d 字节): %s" % (len(body), e))
            return
        if not p:
            try:
                io.open("/volume1/docker/pd_zurg/log/bad_webhook.bin", "wb").write(body)
            except IOError:
                pass
            log("WARN", "事件里找不到 payload 字段(%d 字节),原始体已存 log/bad_webhook.bin" % len(body))
            return
        md = p.get("Metadata") or {}
        ev = p.get("event")
        if ev != "library.new":
            # 记一行:这是"Plex 确实在往这里投递"的唯一可观测证据,否则静默不知死活
            log("EVENT", "收到 %s(%s),不是入库事件,忽略" % (ev, (md.get("title") or "-")[:40]))
            return
        rk = md.get("ratingKey")
        if not rk:
            log("WARN", "library.new 事件里没有 ratingKey,忽略")
            return
        for one in expand(rk, md.get("type")):
            _q.put(str(one))

    def do_GET(self):
        # 给"这个监听器还活着吗"一个能打的地址(不带密钥也答,但只说活着)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"subs_hook alive\n")


def main():
    Handler.secret = hook_secret()
    threading.Thread(target=worker, daemon=True).start()
    log("HOOK", "监听 0.0.0.0:%d,只认路径 /<密钥>(密钥在 %s)" % (PORT, HOOK_CFG))
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
