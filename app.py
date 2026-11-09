# -*- coding: utf-8 -*-
"""
app.py — 高精地图平台后端（FastAPI）

接口：
    GET  /api/health                       健康检查
    GET  /api/stats                        按要素类型统计
    GET  /api/features?bbox=&kind=         要素查询（GeoJSON）
    GET  /api/features/{id}                单个要素
    POST /api/features                     新增要素（需 X-Edit-Token）
    PUT  /api/features/{id}                更新要素（需 X-Edit-Token + body.version）
    GET  /api/tiles/{z}/{x}/{y}.pbf        MVT 瓦片
    GET  /api/tilejson.json                瓦片元数据
    GET  /api/metrics                      Prometheus 文本指标
    GET  /                                 静态前端

启动：
    python -m uvicorn app:app --reload --port 8000

请求链路：Request-ID 中间件 → CORS → 路由 → 统一异常处理器
"""
import json
import logging
import os
import re
import sys
import time
import uuid
from collections import defaultdict
from contextvars import ContextVar
from datetime import datetime, timezone
from threading import Lock

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
import tiles

EDIT_TOKEN = os.environ.get("EDIT_TOKEN", "demo-token")
USE_PG = os.environ.get("DATABASE_URL", "").startswith("postgres")
ALLOWED_GEOM = {"Point", "LineString", "Polygon", "MultiPoint", "MultiLineString", "MultiPolygon"}
ALLOWED_KIND = {"road", "load", "unload", "fuel", "crusher", "stockpile", "geofence", "parking", "dump"}
BACKEND = "postgis" if USE_PG else "sqlite"

# ---------------- 请求 ID：一个请求一个值，日志/错误体共用 ----------------
_request_id = ContextVar("request_id", default="")


def get_request_id():
    return _request_id.get() or ""


# ---------------- 结构化日志：每请求一行 JSON ----------------
_LOG_FIELDS = ("method", "path", "status", "duration_ms", "request_id", "backend")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
        }
        for key in _LOG_FIELDS:
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val
        payload["event"] = record.getMessage()
        if record.exc_info:
            payload["stack"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_logger = logging.getLogger("map.access")
_logger.setLevel(logging.INFO)
_logger.propagate = False
if not _logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(JsonFormatter())
    _logger.addHandler(_handler)

# 关闭 uvicorn 自带 access log：它会再打一条没有 request_id 的访问日志，
# 与中间件这行 JSON 重复；统一由中间件接管，保留 uvicorn 的启动/错误日志。
logging.getLogger("uvicorn.access").disabled = True


# ---------------- 稳定错误码 ----------------
STATUS_TO_CODE = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    503: "DB_UNAVAILABLE",
}


def api_error(status_code, code, message, details=None):
    """业务代码统一用这个抛错，detail 里带稳定 code。"""
    raise HTTPException(status_code=status_code,
                        detail={"code": code, "message": message, "details": details or {}})


def error_body(code, message, details=None, request_id=None):
    rid = request_id or get_request_id()
    return {"error": {
        "code": code,
        "message": message,
        "request_id": rid,
        "details": details or {},
    }}


def error_response(status_code, code, message, details=None):
    rid = get_request_id()
    return JSONResponse(status_code=status_code,
                        content=error_body(code, message, details, rid),
                        headers={"X-Request-ID": rid})


def classify_exception(exc):
    """只把明确的数据库异常归为 503，其余仍是 500；响应体不泄露异常原文。"""
    name = type(exc).__name__
    module = type(exc).__module__ or ""
    text = str(exc)
    if (module.startswith("psycopg2") and name in {"OperationalError", "InterfaceError"}) \
            or (module == "sqlite3" and name == "OperationalError") \
            or text.startswith("[PostGIS"):
        return 503, "DB_UNAVAILABLE", "数据库暂不可用"
    return 500, "INTERNAL_ERROR", "服务器内部错误"


# ---------------- 内存指标（线程安全） ----------------
class Metrics:
    def __init__(self):
        self._lock = Lock()
        self.total = 0
        self.by_status = defaultdict(int)
        self.by_path = defaultdict(int)
        self.tile_count = 0
        self.tile_seconds = 0.0
        self.request_seconds = 0.0

    def record(self, path, status, duration_ms, is_tile):
        seconds = duration_ms / 1000.0
        with self._lock:
            self.total += 1
            self.by_status[str(status)] += 1
            self.by_path[path] += 1
            self.request_seconds += seconds
            if is_tile:
                self.tile_count += 1
                self.tile_seconds += seconds

    def snapshot(self):
        with self._lock:
            return {
                "total": self.total,
                "by_status": dict(self.by_status),
                "by_path": dict(self.by_path),
                "tile_count": self.tile_count,
                "tile_seconds": self.tile_seconds,
                "request_seconds": self.request_seconds,
            }

    def render(self, backend):
        snap = self.snapshot()
        lines = [
            "# HELP map_http_requests_total 请求总数",
            "# TYPE map_http_requests_total counter",
            "map_http_requests_total %d" % snap["total"],
            "# HELP map_http_responses_total 按状态码分组的响应数",
            "# TYPE map_http_responses_total counter",
        ]
        for status in sorted(snap["by_status"]):
            lines.append('map_http_responses_total{status="%s"} %d' % (status, snap["by_status"][status]))
        lines += [
            "# HELP map_http_requests_by_path_total 按路径分组的请求数",
            "# TYPE map_http_requests_by_path_total counter",
        ]
        for path in sorted(snap["by_path"]):
            lines.append('map_http_requests_by_path_total{path="%s"} %d' % (_escape(path), snap["by_path"][path]))
        lines += [
            "# HELP map_tile_requests_total 瓦片请求数",
            "# TYPE map_tile_requests_total counter",
            "map_tile_requests_total %d" % snap["tile_count"],
            "# HELP map_tile_request_duration_seconds 瓦片请求耗时",
            "# TYPE map_tile_request_duration_seconds summary",
            "map_tile_request_duration_seconds_count %d" % snap["tile_count"],
            "map_tile_request_duration_seconds_sum %.6f" % snap["tile_seconds"],
            "# HELP map_http_request_duration_seconds 全部请求耗时",
            "# TYPE map_http_request_duration_seconds summary",
            "map_http_request_duration_seconds_count %d" % snap["total"],
            "map_http_request_duration_seconds_sum %.6f" % snap["request_seconds"],
            "# HELP map_backend_info 当前后端",
            "# TYPE map_backend_info gauge",
            'map_backend_info{backend="%s"} 1' % backend,
        ]
        return "\n".join(lines) + "\n"


def _escape(value):
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


METRICS = Metrics()


# ---------------- 应用与异常处理器 ----------------
app = FastAPI(title="HD Map Platform Demo", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        code = detail["code"]
        message = detail.get("message") or code
        details = detail.get("details") or {}
    else:
        code = STATUS_TO_CODE.get(exc.status_code, "HTTP_ERROR")
        message = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
        details = {}
    response = error_response(exc.status_code, code, message, details)
    if exc.headers:
        response.headers.update(exc.headers)
        response.headers["X-Request-ID"] = get_request_id()
    return response


def _safe_errors(errors):
    out = []
    for item in errors:
        row = {
            "loc": [str(x) for x in item.get("loc", [])],
            "msg": str(item.get("msg", "")),
            "type": item.get("type"),
        }
        if item.get("ctx"):
            row["ctx"] = {k: str(v) for k, v in item["ctx"].items()}
        out.append(row)
    return out


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return error_response(422, "VALIDATION_ERROR", "请求参数校验失败",
                          {"errors": _safe_errors(exc.errors())})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # 日志保留完整堆栈；响应体只给稳定 code 和 request_id
    _logger.error("unhandled_exception", exc_info=exc, extra={
        "method": request.method,
        "path": request.url.path,
        "request_id": get_request_id(),
        "backend": BACKEND,
    })
    status_code, code, message = classify_exception(exc)
    return error_response(status_code, code, message, {"backend": BACKEND})


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    token = _request_id.set(rid)
    start = time.perf_counter()
    response = None
    try:
        response = await call_next(request)
    except Exception as exc:  # 手动调用同一处理器，保证 500 响应也带 X-Request-ID
        response = await unhandled_exception_handler(request, exc)
    finally:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        route = request.scope.get("route")
        path_group = getattr(route, "path", None) or re.sub(r"/\d+", "/{id}", request.url.path)
        is_tile = request.url.path.startswith("/api/tiles/")
        METRICS.record(path_group, getattr(response, "status_code", 500), duration_ms, is_tile)
        _logger.info("http_request", extra={
            "method": request.method,
            "path": request.url.path,
            "status": getattr(response, "status_code", 500),
            "duration_ms": duration_ms,
            "request_id": rid,
            "backend": BACKEND,
        })
        _request_id.reset(token)
    response.headers["X-Request-ID"] = rid
    return response


if USE_PG:
    # 显式配置了 PostGIS 就失败即报错，不静默回退 sqlite
    try:
        db.pg_init_db()
    except Exception as e:
        print("[PostGIS 启动失败] %s: %s" % (type(e).__name__, str(e).strip() or "<empty>"),
              file=sys.stderr)
        raise SystemExit(1)
else:
    db.init_db()


def _rows(bbox=None, kind=None):
    return db.pg_list_features(bbox, kind) if USE_PG else db.list_features(bbox, kind)


def _pg_feature(row):
    # psycopg2 读 JSONB 列时已反序列化成 dict，只有 TEXT 列才是 str；
    # 统一兼容两种形态，避免 json.loads(dict) 抛 TypeError 导致 /api/features 500
    props = row["props"] or {}
    if isinstance(props, str):
        props = json.loads(props)
    props = dict(props)
    props.update({"kind": row["kind"], "name": row["name"]})
    return {"type": "Feature", "id": row["id"], "version": row["version"],
            "properties": props, "geometry": row["geometry"]}


def _validate_payload(kind, geometry):
    if geometry.get("type") not in ALLOWED_GEOM:
        api_error(400, "INVALID_GEOMETRY", "不支持的几何类型: %s" % geometry.get("type"),
                  {"geometry_type": geometry.get("type")})
    if kind not in ALLOWED_KIND:
        api_error(400, "INVALID_KIND", "不支持的要素类型: %s" % kind, {"kind": kind})
    try:
        box = db.geometry_bbox(geometry)
    except Exception as e:
        api_error(400, "INVALID_GEOMETRY", "几何非法: %s" % e,
                  {"geometry_type": geometry.get("type")})
    minx, miny, maxx, maxy = box
    if not (-180 <= minx <= 180 and -180 <= maxx <= 180 and -90 <= miny <= 90 and -90 <= maxy <= 90):
        api_error(400, "INVALID_COORDINATES", "坐标超出经纬度范围（本服务只接受 WGS84）",
                  {"bbox": [minx, miny, maxx, maxy]})
    return box


class FeatureIn(BaseModel):
    type: str = "Feature"
    kind: str
    name: str = ""
    properties: dict = {}
    geometry: dict


class FeatureUpdate(BaseModel):
    version: int
    kind: str
    name: str = ""
    properties: dict = {}
    geometry: dict


@app.get("/api/health")
def health():
    return {"ok": True, "backend": BACKEND}


@app.get("/api/stats")
def stats():
    rows = _rows()
    out = {}
    for r in rows:
        out[r["kind"]] = out.get(r["kind"], 0) + 1
    return {"total": len(rows), "by_kind": out}


@app.get("/api/features")
def features(
    bbox: str = Query(None, description="minx,miny,maxx,maxy"),
    kind: str = Query(None),
):
    box = None
    if bbox:
        try:
            box = tuple(float(x) for x in bbox.split(","))
            assert len(box) == 4
        except Exception:
            api_error(400, "INVALID_BBOX", "bbox 格式应为 minx,miny,maxx,maxy", {"bbox": bbox})
    if USE_PG:
        rows = db.pg_list_features(box, kind)
        return {"type": "FeatureCollection", "features": [_pg_feature(r) for r in rows]}
    return db.to_geojson(db.list_features(box, kind))


@app.get("/api/features/{feature_id}")
def get_feature(feature_id: int):
    if USE_PG:
        row = db.pg_get_feature(feature_id)
        if not row:
            api_error(404, "FEATURE_NOT_FOUND", "要素不存在: %s" % feature_id, {"id": feature_id})
        return _pg_feature(row)
    row = db.get_feature(feature_id)
    if not row:
        api_error(404, "FEATURE_NOT_FOUND", "要素不存在: %s" % feature_id, {"id": feature_id})
    return db.to_geojson([row])["features"][0]


@app.post("/api/features")
def create_feature(f: FeatureIn, x_edit_token: str = Header(default="")):
    # 简单权限：只校验写操作；生产应换成 OAuth2/JWT + 角色
    if x_edit_token != EDIT_TOKEN:
        api_error(401, "INVALID_TOKEN", "无效的编辑令牌")
    _validate_payload(f.kind, f.geometry)
    props = {**f.properties, "kind": f.kind, "name": f.name}
    new_id = (db.pg_insert_feature(f.kind, f.name, props, f.geometry) if USE_PG
              else db.insert_feature(f.kind, f.name, props, f.geometry))
    return {"ok": True, "id": new_id, "version": 1}


@app.put("/api/features/{feature_id}")
def update_feature(feature_id: int, f: FeatureUpdate, x_edit_token: str = Header(default="")):
    if x_edit_token != EDIT_TOKEN:
        api_error(401, "INVALID_TOKEN", "无效的编辑令牌")
    _validate_payload(f.kind, f.geometry)
    props = {**f.properties, "kind": f.kind, "name": f.name}
    new_version = (db.pg_update_feature(feature_id, f.version, f.kind, f.name, props, f.geometry)
                   if USE_PG else
                   db.update_feature(feature_id, f.version, f.kind, f.name, props, f.geometry))
    if new_version is None:
        exists = db.pg_get_feature(feature_id) if USE_PG else db.get_feature(feature_id)
        if not exists:
            api_error(404, "FEATURE_NOT_FOUND", "要素不存在: %s" % feature_id, {"id": feature_id})
        api_error(409, "VERSION_CONFLICT", "版本冲突：该要素已被修改，请刷新后重试",
                  {"id": feature_id, "your_version": f.version})
    return {"ok": True, "id": feature_id, "version": new_version}


@app.get("/api/tiles/{z}/{x}/{y}.pbf")
def tile_mvt(z: int, x: int, y: int):
    if not (0 <= z <= 22):
        api_error(400, "INVALID_TILE_RANGE", "z 超出范围", {"z": z})
    if USE_PG:
        # 生产路径：PostGIS ST_TileEnvelope + ST_AsMVT，瓦片内 layer 名为 site
        try:
            data = db.pg_tile_mvt(z, x, y)
            return Response(content=data, media_type="application/vnd.mapbox-vector-tile",
                            headers={"X-Tile-Source": "postgis-st_asmvt"})
        except Exception:
            pass
    bounds = tiles.tile_bounds(z, x, y)
    rows = [r for r in _rows()
            if not (r["maxx"] < bounds[0] or r["minx"] > bounds[2]
                    or r["maxy"] < bounds[1] or r["miny"] > bounds[3])]
    try:
        data, n = tiles.encode_mvt(rows, bounds)
        return Response(content=data, media_type="application/vnd.mapbox-vector-tile",
                        headers={"X-Feature-Count": str(n), "X-Tile-Source": "python-mvt"})
    except Exception:
        # 降级：返回 GeoJSON，前端/调用方按 content-type 处理
        payload = db.to_geojson(rows)
        payload["tippecanoe"] = {"minzoom": 0, "maxzoom": 22}
        return JSONResponse(payload, headers={"X-Tile-Fallback": "geojson"})


@app.get("/api/tilejson.json")
def tilejson():
    return {
        "tilejson": "3.0.0",
        "name": "site-map",
        "scheme": "xyz",
        "tiles": ["/api/tiles/{z}/{x}/{y}.pbf"],
        "minzoom": 0,
        "maxzoom": 18,
        "bounds": [120.59, 31.29, 120.62, 31.31],
    }


@app.get("/api/metrics")
def metrics():
    return PlainTextResponse(METRICS.render(BACKEND),
                             media_type="text/plain; version=0.0.4; charset=utf-8")


# 静态前端放最后挂载，避免覆盖 /api 路由
app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True), name="static")
