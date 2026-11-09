# -*- coding: utf-8 -*-
"""
server_offline.py — 零依赖后备服务（标准库 http.server）

用途：没有安装 FastAPI/uvicorn 时也能跑通同一套 API（SQLite + GeoJSON 瓦片降级）。
启动：python server_offline.py 8000
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import db
import tiles

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
EDIT_TOKEN = os.environ.get("EDIT_TOKEN", "demo-token")


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path):
        if not os.path.exists(path):
            self._json({"error": "not found"}, 404)
            return
        ext = os.path.splitext(path)[1]
        ctype = {".html": "text/html; charset=utf-8", ".js": "application/javascript",
                 ".css": "text/css", ".png": "image/png", ".geojson": "application/geo+json"}.get(ext, "text/plain")
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        parts = [p for p in u.path.split("/") if p]
        if u.path in ("/", "/index.html"):
            return self._file(os.path.join(STATIC_DIR, "index.html"))
        if u.path == "/api/health":
            return self._json({"ok": True, "backend": "sqlite-offline"})
        if u.path == "/api/stats":
            rows = db.list_features()
            by = {}
            for r in rows:
                by[r["kind"]] = by.get(r["kind"], 0) + 1
            return self._json({"total": len(rows), "by_kind": by})
        if u.path == "/api/features":
            box = tuple(float(x) for x in q["bbox"][0].split(",")) if "bbox" in q else None
            kind = q.get("kind", [None])[0]
            return self._json(db.to_geojson(db.list_features(box, kind)))
        if parts[:2] == ["api", "tiles"] and len(parts) == 5:
            z, x, y = int(parts[2]), int(parts[3]), int(parts[4].split(".")[0])
            bounds = tiles.tile_bounds(z, x, y)
            rows = [r for r in db.list_features()
                    if not (r["maxx"] < bounds[0] or r["minx"] > bounds[2]
                            or r["maxy"] < bounds[1] or r["miny"] > bounds[3])]
            return self._json(db.to_geojson(rows))
        return self._file(os.path.join(STATIC_DIR, u.path.lstrip("/")))

    def do_POST(self):
        if self.path != "/api/features":
            return self._json({"error": "not found"}, 404)
        if self.headers.get("X-Edit-Token", "") != EDIT_TOKEN:
            return self._json({"error": "无效的编辑令牌"}, 401)
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        geom = body.get("geometry", {})
        kind = body.get("kind", "")
        if geom.get("type") not in ("Point", "LineString", "Polygon"):
            return self._json({"error": "不支持的几何类型"}, 400)
        props = {**body.get("properties", {}), "kind": kind, "name": body.get("name", "")}
        new_id = db.insert_feature(kind, body.get("name", ""), props, geom)
        return self._json({"ok": True, "id": new_id})


if __name__ == "__main__":
    db.init_db()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"[offline] http://localhost:{port}  (SQLite, 无 FastAPI 依赖)")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
