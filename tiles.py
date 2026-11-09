# -*- coding: utf-8 -*-
"""
tiles.py — Web Mercator 瓦片计算与 MVT 编码

MVT 编码用 mapbox-vector-tile；未安装时 app.py 自动降级为 GeoJSON 瓦片（内容类型不同）。
"""
import math

from db import geometry_bbox


def tile_bounds(z, x, y):
    """XYZ 瓦片 → (west, south, east, north) 经纬度"""
    n = 2.0 ** z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0

    def lat(ty):
        rad = math.atan(math.sinh(math.pi * (1 - 2 * ty / n)))
        return math.degrees(rad)

    north = lat(y)
    south = lat(y + 1)
    return west, south, east, north


def lonlat_to_tile(lon, lat, z):
    n = 2.0 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def features_in_tile(rows, bounds):
    """bbox 相交判断，返回该瓦片内的要素"""
    west, south, east, north = bounds
    out = []
    for r in rows:
        if r["maxx"] < west or r["minx"] > east or r["maxy"] < south or r["miny"] > north:
            continue
        out.append(r)
    return out


def encode_mvt(rows, bounds, layer_name="site"):
    """把要素编码成 MVT 字节串；rows 需含 geometry(JSON字符串)/props"""
    import json
    import mapbox_vector_tile

    features = []
    for r in rows:
        geom = json.loads(r["geometry"])
        # MVT 只接受点/线/面；跳过其它类型
        if geom.get("type") not in ("Point", "LineString", "Polygon", "MultiPoint",
                                    "MultiLineString", "MultiPolygon"):
            continue
        props = json.loads(r["props"] or "{}")
        props.update({"id": r["id"], "kind": r["kind"], "name": r["name"] or ""})
        # 只保留标量属性，MVT 不支持嵌套对象
        props = {k: v for k, v in props.items() if isinstance(v, (str, int, float, bool))}
        features.append({"geometry": geom, "properties": props})

    if not features:
        return b"", 0

    data = mapbox_vector_tile.encode(
        [{"name": layer_name, "features": features}],
        default_options={"y_coord_down": True, "quantize_bounds": bounds, "extents": 4096},
    )
    return data, len(features)
