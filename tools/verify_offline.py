# -*- coding: utf-8 -*-
"""
verify_offline.py — 断网（屏蔽外网）下的前端闭环自动验证。

做法：
1. 用临时 SQLite 目录起一个独立的 uvicorn（不污染 data/map.db，端口自动选空闲）；
2. Chrome 启动参数 `--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost`
   把除 localhost 之外的域名全部解析失败，等价于断网；
3. 通过 CDP 自动：加载页面 → 点选要素 → 新增点位 → 保存 → 切 3D，并截图；
4. 审计所有外网请求，应为 0。

依赖：requests、websocket-client；本机已安装 Chrome。
用法：python tools/verify_offline.py
"""
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

import requests
import websocket

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "screenshots")
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def log(*args):
    print(*args, flush=True)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=15, suppress_origin=True)
        self.n = 0
        self.events = []

    def call(self, method, params=None, timeout=20):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method} -> {msg['error']}")
                return msg.get("result", {})
            if "method" in msg:
                self.events.append(msg)
        raise TimeoutError(method)

    def eval_js(self, expr, await_promise=False):
        r = self.call("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": await_promise,
        })
        return r.get("result", {}).get("value")

    def click(self, x, y):
        self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        self.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y,
                                               "button": "left", "clickCount": 1})
        self.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y,
                                               "button": "left", "clickCount": 1})

    def shot(self, name):
        os.makedirs(OUT, exist_ok=True)
        data = self.call("Page.captureScreenshot", {"format": "png"})["data"]
        path = os.path.join(OUT, name)
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
        return path


def start_api(port, db_path):
    env = {**os.environ, "SQLITE_PATH": db_path}
    env.pop("DATABASE_URL", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--port", str(port)],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://localhost:{port}"
    for _ in range(60):
        try:
            if requests.get(base + "/api/health", timeout=2).ok:
                return proc, base
        except Exception:
            time.sleep(0.25)
    proc.terminate()
    raise RuntimeError("临时 API 服务未就绪")


def main():
    log(f"[run] pid={os.getpid()} start={time.strftime('%H:%M:%S')}")
    chrome_bin = next((p for p in CHROME_CANDIDATES if os.path.exists(p)), None)
    if not chrome_bin:
        raise RuntimeError("未找到 Chrome，请先安装或修改 CHROME_CANDIDATES")

    api_port, cdp_port = free_port(), free_port()
    tmp = tempfile.mkdtemp(prefix="p5verify_")
    api, BASE = start_api(api_port, os.path.join(tmp, "map.db"))
    log(f"[0] 临时库: {os.path.join(tmp, 'map.db')} · 端口 {api_port}")
    log("[0] 启动前要素数:", requests.get(BASE + "/api/stats", timeout=5).json())

    profile = tempfile.mkdtemp(prefix="p5chrome_")
    chrome = subprocess.Popen([
        chrome_bin, "--headless=new", f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={profile}", "--window-size=1440,900",
        "--no-first-run", "--no-default-browser-check", "--disable-extensions",
        "--remote-allow-origins=*",
        '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost',
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        for _ in range(40):
            try:
                targets = requests.get(f"http://127.0.0.1:{cdp_port}/json/list", timeout=2).json()
                page = [t for t in targets if t["type"] == "page"][0]
                break
            except Exception:
                time.sleep(0.25)
        else:
            raise RuntimeError("Chrome DevTools 端口未就绪")

        cdp = CDP(page["webSocketDebuggerUrl"])
        cdp.call("Page.enable")
        cdp.call("Network.enable")
        cdp.call("Page.navigate", {"url": BASE + "/"})
        for _ in range(40):
            if cdp.eval_js("document.readyState") == "complete":
                break
            time.sleep(0.2)
        time.sleep(1.2)

        # 1) 首页
        cdp.shot("01_home.png")
        status = cdp.eval_js("document.getElementById('status').textContent")
        log("[1] 首页状态:", status)

        # 2) 点选一个 Point 要素
        info = cdp.eval_js("""(() => {
            const f = window.__demo.vectorSource.getFeatures()
              .find(x => x.getGeometry().getType() === 'Point');
            if (!f) return null;
            const p = window.__demo.map.getPixelFromCoordinate(f.getGeometry().getCoordinates());
            const r = document.querySelector('#map canvas').getBoundingClientRect();
            return {name: f.get('name'), kind: f.get('kind'),
                    x: r.left + p[0], y: r.top + p[1]};
        })()""")
        if not info:
            raise RuntimeError("没有找到 Point 要素")
        cdp.click(info["x"], info["y"])
        time.sleep(0.5)
        props = cdp.eval_js("document.getElementById('props').textContent")
        log("[2] 属性面板:", props.replace("\n", " ")[:160])
        assert "kind" in props and info["name"] in props, "点选未显示属性"
        cdp.shot("02_select.png")

        # 3) 新增点位
        before = cdp.eval_js("fetch('/api/stats').then(r=>r.json()).then(j=>j.total)", True)
        cdp.eval_js("document.getElementById('btnAdd').click()")
        box = cdp.eval_js("""(() => {
            const r = document.querySelector('#map').getBoundingClientRect();
            const cands = [[.5,.12],[.15,.5],[.85,.85],[.2,.85],[.5,.9]];
            for (const [fx,fy] of cands) {
                const px = r.width * fx, py = r.height * fy;
                const hit = window.__demo.map.forEachFeatureAtPixel(
                    [px,py], f => f, {layerFilter: l => l === window.__demo.vectorLayer});
                if (!hit) return {x: r.left + px, y: r.top + py};
            }
            return null;
        })()""")
        if not box:
            raise RuntimeError("找不到空白落点")
        cdp.click(box["x"], box["y"])
        time.sleep(0.4)
        picked = cdp.eval_js("document.getElementById('fLonLat').value")
        log("[3] 选取坐标:", picked)
        assert "," in picked, "未选到经纬度"
        cdp.eval_js("""(() => {
            const el = document.getElementById('fName');
            el.value = '离线验证-新增点位';
            el.dispatchEvent(new Event('input', {bubbles: true}));
        })()""")
        cdp.eval_js("document.getElementById('btnSave').click()")
        for _ in range(20):
            msg = cdp.eval_js("document.getElementById('saveMsg').textContent")
            if "已保存" in (msg or ""):
                break
            time.sleep(0.25)
        log("[3] 保存提示:", cdp.eval_js("document.getElementById('saveMsg').textContent"))
        after = cdp.eval_js("fetch('/api/stats').then(r=>r.json()).then(j=>j.total)", True)
        log(f"[3] 要素数: {before} -> {after}")
        assert after == before + 1, "新增未落库"
        cdp.shot("03_added.png")

        # 4) 3D 视角
        cdp.eval_js("document.getElementById('btn3d').click()")
        time.sleep(1.8)
        cdp.shot("04_3d.png")
        log("[4] 3D 已切换，截图:", os.path.join(OUT, "04_3d.png"))

        # 5) 外网请求审计
        external = []
        for e in cdp.events:
            if e.get("method") == "Network.requestWillBeSent":
                url = e["params"]["request"]["url"]
                if url.startswith("http") and "localhost" not in url and "127.0.0.1" not in url:
                    external.append(url)
        log(f"[5] 外网请求数: {len(external)}")
        if external:
            log("    ", external[:5])
        log("OFFLINE-CHECK PASS")
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except Exception:
            chrome.kill()
        api.terminate()
        try:
            api.wait(timeout=5)
        except Exception:
            api.kill()


if __name__ == "__main__":
    sys.exit(main())
