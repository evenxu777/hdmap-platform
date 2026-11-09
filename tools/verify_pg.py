#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
verify_pg.py — PostGIS 路径一键验证（唯一入口，纯标准库 + 项目依赖）

用法（任意目录都可以，脚本自己定位项目根）：
    set DATABASE_URL=postgresql://postgres:密码@127.0.0.1:5432/mapdemo
    .venv\\Scripts\\python.exe tools\\verify_pg.py

四项断言之前先做连接自检：connect → CREATE EXTENSION IF NOT EXISTS postgis（幂等）→
SELECT postgis_version()；失败即整体 FAIL。

四项断言：
    1. GET /api/health 返回 backend=postgis
    2. GET /api/tiles/{z}/{x}/{y}.pbf 返回 200 且 content-type 为
       application/vnd.mapbox-vector-tile
    3. 响应头 X-Tile-Source: postgis-st_asmvt（说明走的是 ST_AsMVT，不是 python-mvt 降级）
    4. mapbox_vector_tile.decode 后存在 layer "site"，且要素数 > 0

进程行为：
    - 8000 端口已监听：复用现有服务，不重复启动，也绝不终止它
    - 未监听：用当前 DATABASE_URL 后台拉起 uvicorn，验证结束只终止本次拉起的进程
    - 失败时打印实际值；临时日志在 %TEMP%，结束即删
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
HOST = "127.0.0.1"
PORT = 8000
BASE = "http://%s:%d" % (HOST, PORT)
# 演示站点中心（120.605, 31.3018）在 z16 的瓦片，库内 9 条种子中有 3 条落在此瓦片
TILE = (16, 54723, 26763)


def port_open():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        return s.connect_ex((HOST, PORT)) == 0
    finally:
        s.close()


def http_get(path, timeout=10):
    req = urllib.request.Request(BASE + path)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # 保留 HTTPMessage（大小写不敏感），ASGI 下发的头是小写
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def wait_health(proc, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            return None  # uvicorn 提前退出
        try:
            status, _, body = http_get("/api/health", timeout=2)
            if status == 200:
                return json.loads(body.decode("utf-8"))
        except Exception:
            pass
        time.sleep(0.5)
    return None


def pg_selfcheck(url):
    """断言前确认连接可用、扩展已安装：CREATE EXTENSION（幂等）→ SELECT postgis_version()"""
    try:
        import psycopg2
        conn = psycopg2.connect(url, connect_timeout=5)
        try:
            cur = conn.cursor()
            cur.execute("CREATE EXTENSION IF NOT EXISTS postgis")
            conn.commit()
            cur.execute("SELECT postgis_version()")
            return cur.fetchone()[0], None
        finally:
            conn.close()
    except Exception as e:
        return None, e


def main():
    url = os.environ.get("DATABASE_URL", "")
    print("verify_pg.py — PostGIS 路径验证")
    print("项目根目录 : %s" % ROOT)
    print("DATABASE_URL: %s" % (url or "<未设置>"))
    if not url.startswith("postgres"):
        print("\n[FAIL] DATABASE_URL 未设置或不以 postgres 开头（当前值: %r）" % url)
        print("       示例: postgresql://postgres:密码@127.0.0.1:5432/mapdemo")
        print("       未设置时服务按默认 SQLite 启动，PostGIS 断言不可能通过。")
        return 2

    version, err = pg_selfcheck(url)
    if err is not None:
        print("\n[FAIL] 连接自检失败（connect + SELECT postgis_version()）")
        try:
            import db
            print(db.pg_error_message(err, url))
        except Exception:
            print("  底层异常: %s: %s" % (type(err).__name__, str(err).strip() or "<empty>"))
        return 1
    print("连接自检   : OK，postgis_version() = %s" % version)

    proc = None
    log_path = os.path.join(tempfile.gettempdir(), "p5_verify_pg_uvicorn.log")
    log_file = None
    reused = port_open()
    if reused:
        print("8000 端口   : 已监听，复用现有服务（不启动、不终止）")
    else:
        print("8000 端口   : 未监听，使用 DATABASE_URL 后台拉起 uvicorn ...")
        log_file = open(log_path, "wb")
        env = os.environ.copy()
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app:app", "--host", HOST, "--port", str(PORT)],
            cwd=ROOT, env=env, stdout=log_file, stderr=subprocess.STDOUT,
        )

    try:
        health = wait_health(proc)
        if health is None:
            print("\n[FAIL] 服务未在 60 秒内就绪（/api/health 不可用）")
            if proc is not None and proc.poll() is not None:
                print("       uvicorn 提前退出，exit=%s" % proc.returncode)
            if log_file is not None and os.path.exists(log_path):
                with open(log_path, "rb") as f:
                    tail = f.read()[-3000:].decode("utf-8", "replace")
                print("\n--- uvicorn 日志（末尾 3000 字节）---")
                print(tail)
                print("--- 日志结束 ---")
            return 1

        z, x, y = TILE
        status, headers, body = http_get("/api/tiles/%d/%d/%d.pbf" % (z, x, y), timeout=15)
        ctype = headers.get("Content-Type", "")
        source = headers.get("X-Tile-Source", "")

        layers, site_count, decode_err = [], 0, None
        try:
            from mapbox_vector_tile import decode
            tile = decode(body)
            layers = sorted(tile.keys())
            site_count = len(tile.get("site", {}).get("features", []))
        except Exception as e:
            decode_err = "%s: %s" % (type(e).__name__, e)

        checks = [
            ("health backend=postgis",
             health.get("backend") == "postgis",
             "backend=%r ok=%r" % (health.get("backend"), health.get("ok"))),
            ("tile 200 + MVT content-type",
             status == 200 and "application/vnd.mapbox-vector-tile" in ctype,
             "status=%s content-type=%r bytes=%d" % (status, ctype, len(body))),
            ("X-Tile-Source=postgis-st_asmvt",
             source == "postgis-st_asmvt",
             "X-Tile-Source=%r" % source),
            ("decode layer 'site' 且要素数>0",
             decode_err is None and site_count > 0,
             "layers=%s site_features=%d%s"
             % (layers, site_count, "" if decode_err is None else " error=" + decode_err)),
        ]

        print("\n断言结果 (tile z=%d x=%d y=%d)" % TILE)
        print("-" * 92)
        failed = 0
        for name, ok, actual in checks:
            print("[%s] %-34s | %s" % ("PASS" if ok else "FAIL", name, actual))
            if not ok:
                failed += 1
        print("-" * 92)
        print("总计: %d/%d PASS" % (len(checks) - failed, len(checks)))
        if failed == 0:
            print("结论: PostGIS 路径在本机实测通过（backend=postgis + ST_AsMVT 原生瓦片）")
            return 0
        print("结论: FAIL（%d 项未通过，见上方实际值）" % failed)
        if reused and health.get("backend") != "postgis":
            print("提示: 8000 端口上的服务不是用 DATABASE_URL 启动的，关闭它后重跑本脚本。")
        return 1
    finally:
        if proc is not None:
            if proc.poll() is None:
                print("\n清理: 终止本次脚本拉起的 uvicorn (pid=%s)" % proc.pid)
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            else:
                print("\n清理: uvicorn 已退出 (pid=%s, exit=%s)" % (proc.pid, proc.returncode))
        if log_file is not None:
            log_file.close()
        if not reused and os.path.exists(log_path):
            try:
                os.remove(log_path)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
