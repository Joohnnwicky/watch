#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""server.py —— 本地看板服务 + 刷新接口(纯标准库)。
- 静态服务整个 investment-news 目录(看板、data、脚本)
- POST /api/refresh → 跑 scripts/fetch.py(抓取+红线+最近N天) 再跑 scripts/digest.py
  (用 llm.config.json 配的大模型出「今日要点」+翻译),完成后返回 JSON。前端按钮转圈等它。
跑法: python3 server.py [port]   默认 8793
"""
import os, sys, json, subprocess, tempfile
from urllib.parse import urlparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8793
SOURCES = os.path.join(HERE, "sources.json")


def urllib_host(url):
    try: return urlparse(url).netloc
    except Exception: return url


def child_env():
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    # 保证子进程能找到 claude(订阅模式)
    extra = "/opt/homebrew/bin:/usr/local/bin:" + os.path.expanduser("~/.local/bin")
    env["PATH"] = extra + ":" + env.get("PATH", "")
    return env


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=HERE, **k)

    def log_message(self, *a):
        pass

    def _refresh(self):
        try:
            py = sys.executable
            env = child_env()
            # encoding 必须显式指定 utf-8：child_env() 已给子进程设了 PYTHONUTF8=1
            # （强制它输出 UTF-8），父进程若按 locale 解码，中文 Windows 上就是 GBK，
            # 必然 UnicodeDecodeError（issue #4）。errors="replace" 兜底异常字节。
            r1 = subprocess.run([py, "scripts/fetch.py"], cwd=HERE, env=env,
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=600)
            r2 = subprocess.run([py, "scripts/digest.py"], cwd=HERE, env=env,
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=1200)
            ok = (r2.returncode == 0 and r1.returncode == 0)
            payload = {"ok": ok, "fetch": (r1.stdout or "")[-500:], "digest": (r2.stdout or "")[-500:]}
            if not ok:
                payload["error"] = ((r2.stderr or "") + (r1.stderr or ""))[-500:]
            code = 200 if ok else 500
        except Exception as e:
            payload, code = {"ok": False, "error": str(e)}, 500
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_sources(self):
        try:
            return json.load(open(SOURCES, encoding="utf-8")), None
        except Exception as e:
            return None, str(e)

    def _write_sources(self, data):
        # 原子写:先写临时文件再 os.replace,避免半写损坏 sources.json
        try:
            fd, tmp = tempfile.mkstemp(dir=HERE, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, SOURCES)
            return None
        except Exception as e:
            try: os.unlink(tmp)
            except Exception: pass
            return str(e)

    def _sources_get(self):
        data, err = self._read_sources()
        if err: return self._json(500, {"ok": False, "error": err})
        return self._json(200, {"ok": True, "countries": data.get("countries", []),
                                "branches": data.get("branches", []),
                                "sources": data.get("sources", [])})

    def _sources_add(self):
        body = self._read_body()
        if not body: return self._json(400, {"ok": False, "error": "空请求体"})
        try: req = json.loads(body)
        except Exception: return self._json(400, {"ok": False, "error": "非法JSON"})
        url = (req.get("url") or "").strip()
        country = (req.get("country") or "").strip()
        branch = (req.get("branch") or "").strip()
        name = (req.get("name") or "").strip()
        if not url: return self._json(400, {"ok": False, "error": "URL 为空"})

        # 调 discover 发现 feed(子进程,继承环境含 LLM_API_KEY)
        env = child_env()
        py = sys.executable
        try:
            r = subprocess.run([py, "scripts/discover.py", url], cwd=HERE, env=env,
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=90)
        except subprocess.TimeoutExpired:
            return self._json(504, {"ok": False, "error": "发现超时(>90s)"})
        out = r.stdout.strip()
        # discover.py 末尾打印 JSON
        a, b = out.find("{"), out.rfind("}")
        if a < 0 or b < 0:
            return self._json(500, {"ok": False, "error": "发现失败:%s" % (r.stderr or "")[:300]})
        try: d = json.loads(out[a:b+1])
        except Exception: return self._json(500, {"ok": False, "error": "发现返回非JSON"})
        if not d.get("ok"):
            # 返回发现详情供前端展示(no_feed 也把分类建议带上)
            return self._json(200, {"ok": False, "discovered": d})

        feed_url = d["feed_url"]
        # 校验:已存在同 feed_url 的源不重复加
        data, err = self._read_sources()
        if err: return self._json(500, {"ok": False, "error": err})
        if any(s.get("url") == feed_url for s in data.get("sources", [])):
            return self._json(200, {"ok": False, "error": "该源已存在", "feed_url": feed_url})
        # name:用户填 > discover 抓的 > 域名
        if not name: name = d.get("name", "") or urllib_host(feed_url)
        # country/branch:用户选 > discover/LLM 建议;都没给则不写(默认空)
        if not country: country = d.get("country", "") or ""
        if not branch: branch = d.get("branch", "") or ""
        src = {"name": name, "type": "rss", "url": feed_url}
        if country: src["country"] = country
        if branch: src["branch"] = branch
        else: src["branch"] = "press"   # 兜底分类,避免 fetch 找不到桶
        data.setdefault("sources", []).append(src)
        werr = self._write_sources(data)
        if werr: return self._json(500, {"ok": False, "error": werr})
        return self._json(200, {"ok": True, "source": src, "discovered": d})

    def _sources_del(self):
        body = self._read_body()
        try: req = json.loads(body) if body else {}
        except Exception: req = {}
        url = (req.get("url") or "").strip()
        name = (req.get("name") or "").strip()
        if not url and not name:
            return self._json(400, {"ok": False, "error": "需提供 url 或 name"})
        data, err = self._read_sources()
        if err: return self._json(500, {"ok": False, "error": err})
        before = len(data.get("sources", []))
        # 删掉匹配的源(保留不匹配的)。url 精确匹配优先,name 也精确
        data["sources"] = [s for s in data.get("sources", [])
                           if not ((url and s.get("url") == url) or (name and s.get("name") == name))]
        if len(data["sources"]) == before:
            return self._json(404, {"ok": False, "error": "未找到匹配的源"})
        werr = self._write_sources(data)
        if werr: return self._json(500, {"ok": False, "error": werr})
        return self._json(200, {"ok": True, "removed": before - len(data["sources"])})

    def _read_body(self):
        try:
            ln = int(self.headers.get("Content-Length", 0))
            return self.rfile.read(ln).decode("utf-8") if ln else ""
        except Exception:
            return ""

    def do_POST(self):
        if self.path.startswith("/api/refresh"):
            return self._refresh()
        if self.path.startswith("/api/sources"):
            return self._sources_add()
        self.send_error(404)

    def do_DELETE(self):
        if self.path.startswith("/api/sources"):
            return self._sources_del()
        self.send_error(404)

    def do_GET(self):
        if self.path.startswith("/api/sources"):
            return self._sources_get()
        # /api/refresh 只走 POST:GET 会跑 fetch.py+claude 子进程,若可 GET 触发则易被
        # <img>/跳转做 CSRF。这里让它落到静态处理(404),仅 do_POST 才真正刷新。
        return super().do_GET()


if __name__ == "__main__":
    print("看板服务已启动: http://localhost:%d/index.html   (Ctrl+C 停止)" % PORT)
    # 只绑回环:看板+/api/refresh 会跑本机子进程(fetch.py/claude),绝不能对局域网开放。
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
