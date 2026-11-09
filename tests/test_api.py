# -*- coding: utf-8 -*-
"""项目⑤ API 回归测试：SQLite 临时库，不依赖 PostGIS / 外部网络。"""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 必须在 import app 之前指定临时库，避免污染 data/map.db
_TMP = tempfile.mkdtemp(prefix="p5test_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)  # 进程退出清理临时库
os.environ["SQLITE_PATH"] = str(Path(_TMP) / "test.db")
os.environ.pop("DATABASE_URL", None)

from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402
import tiles  # noqa: E402

client = TestClient(app_module.app)
TOKEN = {"X-Edit-Token": os.environ.get("EDIT_TOKEN", "demo-token")}


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["backend"] == "sqlite"


def test_tile_status_and_content_type():
    z, x, y = 16, *tiles.lonlat_to_tile(120.606, 31.303, 16)
    r = client.get(f"/api/tiles/{z}/{x}/{y}.pbf")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/vnd.mapbox-vector-tile")
    assert len(r.content) > 0
    assert r.headers.get("x-tile-source") == "python-mvt"


def test_features_geojson_structure():
    r = client.get("/api/features")
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "FeatureCollection"
    assert len(body["features"]) >= 9
    for f in body["features"]:
        assert f["type"] == "Feature"
        assert "id" in f
        assert f["version"] >= 1
        assert "kind" in f["properties"]
        assert f["geometry"]["type"] in {"Point", "LineString", "Polygon"}


def test_create_feature_then_query():
    before = client.get("/api/stats").json()["total"]
    payload = {
        "type": "Feature",
        "kind": "load",
        "name": "pytest-新增点位",
        "properties": {"source": "pytest"},
        "geometry": {"type": "Point", "coordinates": [120.6061, 31.3031]},
    }
    r = client.post("/api/features", json=payload, headers=TOKEN)
    assert r.status_code == 200
    new_id = r.json()["id"]
    assert isinstance(new_id, int)
    assert r.json()["version"] == 1

    after = client.get("/api/stats").json()["total"]
    assert after == before + 1

    q = client.get("/api/features", params={"bbox": "120.605,31.302,120.607,31.304"})
    assert q.status_code == 200
    hits = [f for f in q.json()["features"] if f["properties"].get("name") == "pytest-新增点位"]
    assert hits, "新增要素未出现在 bbox 查询结果中"


def test_create_with_bad_token_401():
    payload = {
        "kind": "load", "name": "bad-token",
        "geometry": {"type": "Point", "coordinates": [120.6, 31.3]},
    }
    r = client.post("/api/features", json=payload, headers={"X-Edit-Token": "wrong"})
    assert r.status_code == 401


def test_invalid_geometry_400():
    payload = {
        "kind": "load", "name": "bad-geom",
        "geometry": {"type": "Circle", "coordinates": [120.6, 31.3]},
    }
    r = client.post("/api/features", json=payload, headers=TOKEN)
    assert r.status_code == 400


def test_lonlat_to_tile_roundtrip():
    lon, lat, z = 120.606, 31.303, 16
    x, y = tiles.lonlat_to_tile(lon, lat, z)
    west, south, east, north = tiles.tile_bounds(z, x, y)
    assert west <= lon <= east
    assert south <= lat <= north
    # 已知值固定，防止投影公式被误改
    assert (x, y) == (54723, 26762)


def test_request_id_on_404_error():
    r = client.get("/api/features/99999")
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["code"] == "FEATURE_NOT_FOUND"
    assert body["error"]["request_id"]
    assert r.headers["x-request-id"] == body["error"]["request_id"]


def test_custom_request_id_echoed():
    rid = "trace-abc-123"
    r = client.get("/api/health", headers={"X-Request-ID": rid})
    assert r.status_code == 200
    assert r.headers["x-request-id"] == rid


def test_validation_error_envelope():
    r = client.post("/api/features", json={"kind": "load", "name": "缺少几何"}, headers=TOKEN)
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["request_id"] == r.headers["x-request-id"]
    assert body["error"]["details"]["errors"]


def test_update_with_stale_version_409():
    payload = {
        "kind": "load", "name": "pytest-乐观锁",
        "properties": {"source": "pytest-lock"},
        "geometry": {"type": "Point", "coordinates": [120.6062, 31.3032]},
    }
    created = client.post("/api/features", json=payload, headers=TOKEN).json()
    fid = created["id"]
    update = dict(payload, version=created["version"], name="pytest-乐观锁-v2")

    ok = client.put("/api/features/%d" % fid, json=update, headers=TOKEN)
    assert ok.status_code == 200
    assert ok.json()["version"] == 2

    stale = dict(update, version=created["version"], name="pytest-乐观锁-v3")
    conflict = client.put("/api/features/%d" % fid, json=stale, headers=TOKEN)
    assert conflict.status_code == 409
    body = conflict.json()
    assert body["error"]["code"] == "VERSION_CONFLICT"
    assert body["error"]["request_id"] == conflict.headers["x-request-id"]


def test_metrics_prometheus_counters():
    # 自成一体：先制造 404 与 409，再看计数器，避免依赖用例执行顺序
    client.get("/api/features/99999")
    payload = {
        "kind": "load", "name": "pytest-metrics",
        "properties": {"source": "pytest-metrics"},
        "geometry": {"type": "Point", "coordinates": [120.6065, 31.3035]},
    }
    created = client.post("/api/features", json=payload, headers=TOKEN).json()
    body = dict(payload, version=created["version"])
    client.put("/api/features/%d" % created["id"], json=body, headers=TOKEN)
    client.put("/api/features/%d" % created["id"], json=body, headers=TOKEN)

    r = client.get("/api/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    text = r.text
    assert "map_http_requests_total" in text
    assert 'map_http_responses_total{status="404"}' in text
    assert 'map_http_responses_total{status="409"}' in text
    assert "map_tile_requests_total" in text
    assert 'map_backend_info{backend="sqlite"} 1' in text
