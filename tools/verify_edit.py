# -*- coding: utf-8 -*-
"""
verify_edit.py — 前端编辑 + 乐观锁（版本号）浏览器实测

做法（与 tools/verify_offline.py 同一套 Chrome + CDP 思路）：
1. 用临时 SQLite 目录起独立 uvicorn（端口自动选空闲，不污染 data/map.db）；
2. 无头 Chrome 打开首页，真实点选一个已知 POI；
3. 改名 → 保存 → 校验落库版本号 → 刷新后再点选 → 逐字段比对属性；
4. 页面内 fetch 携带旧 version 触发 409；
5. 再制造一次真实的"别人先改了"冲突，截图页面上的 409 提示。

依赖：requests、websocket-client；本机已安装 Chrome。
用法：python tools/verify_edit.py

失败时保留临时库与截图目录并打印路径；全部通过才清理。
"""
import base64
import json
import os
import shutil
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
TARGET_NAME = "装载点-A"
NEW_NAME = "装载点-A-改名验证"
# 这些字段由后端生成/由 API 顶层承载，不参与"业务属性是否丢失"的比对
RESERVED = {"kind", "name", "status", "updated_at", "version", "geometry", "id"}


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

    def call(self, method, params=None, timeout=20):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s -> %s" % (method, msg["error"]))
                return msg.get("result", {})
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
    base = "http://localhost:%d" % port
    for _ in range(80):
        try:
            if requests.get(base + "/api/health", timeout=2).ok:
                return proc, base
        except Exception:
            pass
        time.sleep(0.25)
    proc.terminate()
    raise RuntimeError("临时 API 服务未就绪")


def wait_features(cdp, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        n = cdp.eval_js("(window.__demo && window.__demo.vectorSource.getFeatures().length) || 0")
        if n:
            return n
        time.sleep(0.25)
    return 0


def wait_ready(cdp, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cdp.eval_js("document.readyState") == "complete":
            return True
        time.sleep(0.2)
    return False


def point_pixel(cdp, name):
    return cdp.eval_js("""(() => {
        const name = %s;
        const f = window.__demo.vectorSource.getFeatures()
            .find(x => x.get('name') === name && x.getGeometry().getType() === 'Point');
        if (!f) return null;
        const p = window.__demo.map.getPixelFromCoordinate(f.getGeometry().getCoordinates());
        const r = document.querySelector('#map canvas').getBoundingClientRect();
        return {x: r.left + p[0], y: r.top + p[1], id: f.getId(), version: f.get('version')};
    })()""" % json.dumps(name, ensure_ascii=False))


def click_until_selected(cdp, name):
    """点要素；误点到相邻要素时用小偏移重试"""
    info = point_pixel(cdp, name)
    if not info:
        return None
    for dx, dy in [(0, 0), (7, 0), (-7, 0), (0, 7), (0, -7), (10, 10)]:
        cdp.click(info["x"] + dx, info["y"] + dy)
        time.sleep(0.45)
        props = cdp.eval_js("document.getElementById('props').textContent") or ""
        if name in props:
            return info
    return None


def panel_props(cdp):
    text = cdp.eval_js("document.getElementById('props').textContent") or ""
    try:
        return json.loads(text), text
    except Exception:
        return {}, text


def main():
    rows = []

    def check(name, expected, actual, ok=None):
        if ok is None:
            ok = expected == actual
        rows.append({"name": name, "expected": expected, "actual": actual, "ok": bool(ok)})
        log("[%s] %-28s 期望=%s 实际=%s" % ("PASS" if ok else "FAIL", name, expected, actual))
        return ok

    chrome_bin = next((p for p in CHROME_CANDIDATES if os.path.exists(p)), None)
    if not chrome_bin:
        log("[FAIL] 未找到 Chrome，已尝试：")
        for p in CHROME_CANDIDATES:
            log("       " + p)
        return 2

    api_port, cdp_port = free_port(), free_port()
    tmp = tempfile.mkdtemp(prefix="p5edit_")
    profile = tempfile.mkdtemp(prefix="p5chrome_")
    db_path = os.path.join(tmp, "map.db")
    screenshot_paths = []
    api = chrome = None
    passed = False
    cdp = None
    try:
        api, BASE = start_api(api_port, db_path)
        log("[0] 临时库: %s · 端口 %d" % (db_path, api_port))
        log("[0] 启动前要素数:", requests.get(BASE + "/api/stats", timeout=5).json())

        fc = requests.get(BASE + "/api/features", timeout=5).json()
        target = next((f for f in fc["features"]
                       if f["properties"].get("name") == TARGET_NAME), None)
        if target is None:
            raise RuntimeError("种子数据里找不到 %s" % TARGET_NAME)
        fid, v0 = target["id"], target["version"]
        props0 = target["properties"]
        log("[0] 目标要素: id=%s version=%s name=%s props=%s"
            % (fid, v0, props0.get("name"), json.dumps(props0, ensure_ascii=False)))

        chrome = subprocess.Popen([
            chrome_bin, "--headless=new", "--remote-debugging-port=%d" % cdp_port,
            "--user-data-dir=%s" % profile, "--window-size=1440,900",
            "--no-first-run", "--no-default-browser-check", "--disable-extensions",
            "--remote-allow-origins=*", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        page = None
        for _ in range(40):
            try:
                targets = requests.get("http://127.0.0.1:%d/json/list" % cdp_port, timeout=2).json()
                page = next(t for t in targets if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.25)
        if not page:
            raise RuntimeError("Chrome DevTools 端口未就绪")

        cdp = CDP(page["webSocketDebuggerUrl"])
        cdp.call("Page.enable")
        cdp.call("Page.navigate", {"url": BASE + "/"})
        wait_ready(cdp)
        n = wait_features(cdp)
        status = cdp.eval_js("document.getElementById('status').textContent") or ""
        check("1. 首页加载要素", "要素>0 且含后端", "要素数=%d / %s" % (n, status), n > 0 and "后端" in status)

        # ---- 点选已知 POI ----
        info = click_until_selected(cdp, TARGET_NAME)
        panel, panel_text = panel_props(cdp)
        check("2. 点选显示当前 name", TARGET_NAME, panel.get("name"), panel.get("name") == TARGET_NAME)
        check("3. 点选显示属性 capacity_t", props0.get("capacity_t"), panel.get("capacity_t"),
              panel.get("capacity_t") == props0.get("capacity_t"))

        btn_text = cdp.eval_js("document.getElementById('btnSave').textContent") or ""
        check("4. 进入编辑态", "更新到后端（v%d）" % v0, btn_text, btn_text == "更新到后端（v%d）" % v0)
        form_name = cdp.eval_js("document.getElementById('fName').value") or ""
        check("5. 表单回填原名", TARGET_NAME, form_name, form_name == TARGET_NAME)
        screenshot_paths.append(cdp.shot("05_edit_mode.png"))

        # ---- 改名保存 ----
        cdp.eval_js("""(() => {
            const el = document.getElementById('fName');
            el.value = %s;
            el.dispatchEvent(new Event('input', {bubbles: true}));
        })()""" % json.dumps(NEW_NAME, ensure_ascii=False))
        cdp.eval_js("document.getElementById('btnSave').click()")
        msg = ""
        for _ in range(30):
            msg = cdp.eval_js("document.getElementById('saveMsg').textContent") or ""
            if "已更新" in msg or "已保存" in msg or "失败" in msg:
                break
            time.sleep(0.25)
        check("6. 保存提示", "已更新（含 version=2）", msg, "已更新" in msg)
        panel_after, _ = panel_props(cdp)
        check("7. 保存后属性面板同步", "%s / v%d" % (NEW_NAME, v0 + 1),
              "%s / v%s" % (panel_after.get("name"), panel_after.get("version")),
              panel_after.get("name") == NEW_NAME and panel_after.get("version") == v0 + 1)
        screenshot_paths.append(cdp.shot("06_edit_saved.png"))

        # ---- 落库校验 ----
        saved = requests.get(BASE + "/api/features/%d" % fid, timeout=5).json()
        saved_props = saved["properties"]
        check("8. 落库 name", NEW_NAME, saved_props.get("name"), saved_props.get("name") == NEW_NAME)
        check("9. 落库 version", v0 + 1, saved.get("version"), saved.get("version") == v0 + 1)

        # ---- 属性逐字段比对 ----
        extra_keys = sorted(k for k in props0 if k not in RESERVED)
        if not extra_keys:
            check("10. 属性不丢（业务字段）", "至少 1 个业务字段", "0 个", False)
        for key in extra_keys:
            check("10. 属性不丢 %s" % key, props0.get(key), saved_props.get(key),
                  saved_props.get(key) == props0.get(key))

        # ---- 刷新后仍为新名（证明不是前端状态） ----
        cdp.call("Page.reload", {"ignoreCache": True})
        wait_ready(cdp)
        wait_features(cdp)
        time.sleep(0.6)
        again = click_until_selected(cdp, NEW_NAME)
        panel2, _ = panel_props(cdp)
        check("11. 刷新后点选仍为新名", NEW_NAME, panel2.get("name"),
              again is not None and panel2.get("name") == NEW_NAME)

        # ---- 页面内 fetch 携带旧 version ----
        stale = cdp.eval_js("""(async () => {
            const id = %d;
            const cur = await (await fetch('/api/features/' + id)).json();
            const body = {version: %d, kind: cur.properties.kind, name: cur.properties.name,
                          properties: cur.properties, geometry: cur.geometry};
            const r = await fetch('/api/features/' + id, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json',
                          'X-Edit-Token': document.getElementById('token').value},
                body: JSON.stringify(body)
            });
            const j = await r.json();
            return {status: r.status, code: (j.error || {}).code,
                    request_id: (j.error || {}).request_id,
                    header_rid: r.headers.get('X-Request-ID')};
        })()""" % (fid, v0), await_promise=True)
        check("12. 旧 version PUT 返回 409", 409, stale.get("status"), stale.get("status") == 409)
        check("13. 409 错误码", "VERSION_CONFLICT", stale.get("code"),
              stale.get("code") == "VERSION_CONFLICT")
        check("14. 409 request_id 与响应头一致", stale.get("header_rid"), stale.get("request_id"),
              bool(stale.get("request_id")) and stale.get("header_rid") == stale.get("request_id"))

        # ---- 真实冲突：页面停留在旧 version，另一个客户端先改了 ----
        info3 = click_until_selected(cdp, NEW_NAME)
        editing_version = cdp.eval_js("editing && editing.version")
        cur = requests.get(BASE + "/api/features/%d" % fid, timeout=5).json()
        other = {
            "version": cur["version"], "kind": cur["properties"]["kind"],
            "name": cur["properties"]["name"], "properties": cur["properties"],
            "geometry": cur["geometry"],
        }
        bumped = requests.put(BASE + "/api/features/%d" % fid, json=other,
                              headers={"X-Edit-Token": "demo-token"}, timeout=5)
        log("[3] 外部客户端先改：HTTP %d -> version=%s"
            % (bumped.status_code, bumped.json().get("version")))
        cdp.eval_js("document.getElementById('btnSave').click()")
        msg2 = ""
        for _ in range(30):
            msg2 = cdp.eval_js("document.getElementById('saveMsg').textContent") or ""
            if "失败" in msg2:
                break
            time.sleep(0.25)
        check("15. 页面展示 409 提示", "保存失败 + 版本冲突",
              msg2, "保存失败" in msg2 and "版本冲突" in msg2)
        check("16. 冲突前页面 version 落后", True, "页面=%s 库=%s"
              % (editing_version, bumped.json().get("version")),
              editing_version == v0 + 1 and bumped.json().get("version") == v0 + 2)
        screenshot_paths.append(cdp.shot("07_edit_conflict.png"))

        passed = all(r["ok"] for r in rows)
    except Exception as e:
        log("[ERROR] %s: %s" % (type(e).__name__, e))
        rows.append({"name": "执行过程", "expected": "无异常", "actual": "%s: %s" % (type(e).__name__, e),
                     "ok": False})
    finally:
        if chrome is not None:
            chrome.terminate()
            try:
                chrome.wait(timeout=5)
            except Exception:
                chrome.kill()
        if api is not None:
            api.terminate()
            try:
                api.wait(timeout=5)
            except Exception:
                api.kill()

    log("")
    log("=" * 86)
    for r in rows:
        log("[%s] %-34s 期望=%s | 实际=%s"
            % ("PASS" if r["ok"] else "FAIL", r["name"], r["expected"], r["actual"]))
    log("=" * 86)
    ok_count = sum(1 for r in rows if r["ok"])
    log("总计: %d/%d PASS" % (ok_count, len(rows)))
    for p in screenshot_paths:
        log("截图: %s" % p)

    if passed:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(profile, ignore_errors=True)
        log("清理: 临时库与 Chrome profile 已删除")
        log("EDIT-CHECK PASS")
        return 0
    log("现场保留（便于排查）：")
    log("  临时库目录: %s" % tmp)
    log("  Chrome profile: %s" % profile)
    log("EDIT-CHECK FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
