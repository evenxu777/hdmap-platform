#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
import_shapes.py — GeoJSON 拆分 → Shapefile → PostGIS staging → features

链路（仅 DATABASE_URL 指向 PostGIS 时使用）：
    data/site.geojson
      → 按几何类型拆成 point / line / polygon 三套 Shapefile（data/site/shapefile/）
      → ogr2ogr 生成 PGDUMP SQL
      → 执行到 staging.site_* 表
      → SQL 合并进 features（按 props->>'shape_key' 幂等）

说明：本机 GDAL 3.9.2 的 ogr2ogr 未编译 PostgreSQL 驱动（--formats 只有 PGDUMP），
所以用 PGDUMP 落 staging；执行仍走 psycopg2，不引入新依赖。

用法：
    set DATABASE_URL=postgresql://postgres:占位符@127.0.0.1:5432/mapdemo
    .venv\\Scripts\\python.exe tools\\import_shapes.py

可用环境变量覆盖工具路径：
    PGBIN           默认 C:\\Program Files\\PostgreSQL\\17\\bin
    POSTGIS_BUNDLE  默认 F:\\001download\\...\\postgis-bundle-pg17-3.6.2x64
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from urllib.parse import unquote, urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
SRC = os.path.join(ROOT, "data", "site.geojson")
SHP_DIR = os.path.join(ROOT, "data", "site", "shapefile")
STAGING = "staging"

PG_BIN = os.environ.get("PGBIN", r"C:\Program Files\PostgreSQL\17\bin")
BUNDLE = os.environ.get("POSTGIS_BUNDLE",
                        r"F:\001download\001application\postgis-bundle-pg17-3.6.2x64\postgis-bundle-pg17-3.6.2x64")

# 几何类型 → 拆分集合 / Shapefile 几何类型 / staging 表
GEOM_CLASS = {
    "Point": ("point", "POINT"),
    "MultiPoint": ("point", "MULTIPOINT"),
    "LineString": ("line", "LINESTRING"),
    "MultiLineString": ("line", "MULTILINESTRING"),
    "Polygon": ("polygon", "POLYGON"),
    "MultiPolygon": ("polygon", "MULTIPOLYGON"),
}
TABLE = {"point": "site_point", "line": "site_line", "polygon": "site_polygon"}


def first_existing(paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def tool_env():
    """ogr2ogr 依赖 GDAL_DATA / PROJ_LIB；本机 bundle 里两套数据都在"""
    env = os.environ.copy()
    gdal_data = first_existing([os.path.join(BUNDLE, "gdal-data"),
                                os.path.join(os.path.dirname(PG_BIN), "gdal-data")])
    proj_lib = first_existing([os.path.join(BUNDLE, "share", "contrib", "postgis-3.6", "proj"),
                               os.path.join(os.path.dirname(PG_BIN), "share", "contrib", "postgis-3.6", "proj")])
    if gdal_data:
        env["GDAL_DATA"] = gdal_data
    if proj_lib:
        env["PROJ_LIB"] = proj_lib
    env["PATH"] = os.path.join(BUNDLE, "bin") + ";" + PG_BIN + ";" + env.get("PATH", "")
    return env


def find_ogr2ogr():
    """先试 PG 安装目录，再试 PostGIS bundle；两边都必须能实际跑 --version"""
    for exe in [os.path.join(PG_BIN, "ogr2ogr.exe"), os.path.join(BUNDLE, "bin", "ogr2ogr.exe")]:
        if not os.path.exists(exe):
            continue
        env = tool_env()
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, env=env)
        if r.returncode == 0:
            return exe, env, r.stdout.strip()
    return None, None, None


def redact_dsn(dsn):
    p = urlsplit(dsn)
    return "scheme=%s host=%s port=%s dbname=%s user=%s（密码已隐藏）" % (
        p.scheme, p.hostname, p.port, unquote(p.path.lstrip("/")), unquote(p.username or ""))


def split_source():
    """按几何类型拆分；统计空几何/未知类型，不静默丢弃"""
    with open(SRC, encoding="utf-8") as f:
        fc = json.load(f)
    parts = {c: [] for c in TABLE}
    seq = Counter()
    stats = Counter()
    skipped = []
    total = len(fc.get("features", []))
    for idx, feat in enumerate(fc.get("features", [])):
        geom = feat.get("geometry") or {}
        gtype = geom.get("type")
        if not geom.get("coordinates"):
            stats["空几何"] += 1
            skipped.append((idx, gtype, "空几何"))
            continue
        if gtype not in GEOM_CLASS:
            stats["未知几何类型"] += 1
            skipped.append((idx, gtype, "未知几何类型"))
            continue
        cls = GEOM_CLASS[gtype][0]
        seq[cls] += 1
        props = dict(feat.get("properties") or {})
        props["shape_key"] = "sf-%s-%02d" % (cls, seq[cls])
        # DBF 字段名最多 10 字符且会截断/改名，完整属性放进 props_json 保留语义
        props["props_json"] = json.dumps(feat.get("properties") or {}, ensure_ascii=False)
        parts[cls].append({"type": "Feature", "properties": props, "geometry": geom})
        stats["已拆分:" + cls] += 1
    return parts, stats, skipped, total


def write_shapefiles(parts, tmpdir, ogr, env):
    """每个类型一个 GeoJSON → 一套 .shp/.shx/.dbf/.prj；几何类型混用时 PROMOTE_TO_MULTI"""
    os.makedirs(SHP_DIR, exist_ok=True)
    produced = {}
    for cls, feats in parts.items():
        if not feats:
            produced[cls] = []
            continue
        src = os.path.join(tmpdir, cls + ".geojson")
        with open(src, "w", encoding="utf-8") as f:
            json.dump({"type": "FeatureCollection", "features": feats}, f, ensure_ascii=False)
        shp = os.path.join(SHP_DIR, cls + ".shp")
        for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
            p = os.path.join(SHP_DIR, cls + ext)
            if os.path.exists(p):
                os.remove(p)
        nlt = "PROMOTE_TO_MULTI" if any(
            (f["geometry"].get("type") or "").startswith("Multi") for f in feats) else GEOM_CLASS[feats[0]["geometry"]["type"]][1]
        r = subprocess.run([ogr, "-f", "ESRI Shapefile", "-lco", "ENCODING=UTF-8",
                            shp, src, "-nln", cls, "-nlt", nlt],
                           capture_output=True, text=True, env=env)
        if r.returncode != 0:
            raise RuntimeError("Shapefile 生成失败（%s）：%s" % (cls, (r.stderr or r.stdout).strip()))
        files = []
        for ext in (".shp", ".shx", ".dbf", ".prj"):
            p = os.path.join(SHP_DIR, cls + ext)
            if not os.path.exists(p):
                raise RuntimeError("缺少 Shapefile 组件：%s" % p)
            files.append((os.path.basename(p), os.path.getsize(p)))
        with open(os.path.join(SHP_DIR, cls + ".prj"), encoding="utf-8", errors="replace") as f:
            prj = f.read()
        if "WGS_1984" not in prj and "WGS 84" not in prj:
            raise RuntimeError("坐标系不是 WGS84：%s" % prj[:80])
        produced[cls] = files
    return produced


def pgdump_sql(ogr, env, shp, sql_path, table):
    r = subprocess.run([ogr, "-f", "PGDUMP", sql_path, shp,
                        "-lco", "SCHEMA=" + STAGING, "-lco", "GEOMETRY_NAME=geom",
                        "-lco", "FID=ogc_fid", "-nln", table],
                       capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError("PGDUMP 生成失败（%s）：%s" % (table, (r.stderr or r.stdout).strip()))


def run_sql_file(conn, path):
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    # PGDUMP 每份 SQL 都带 CREATE SCHEMA（无 IF NOT EXISTS）；schema 已由脚本预先建好
    sql = "".join(line for line in lines if not line.strip().upper().startswith("CREATE SCHEMA"))
    cur = conn.cursor()
    cur.execute(sql)


def staging_stats(conn, table):
    cur = conn.cursor()
    cur.execute("""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE geom IS NULL) AS null_geom,
               count(*) FILTER (WHERE geom IS NOT NULL AND ST_IsEmpty(geom)) AS empty_geom,
               count(*) FILTER (WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom) AND ST_SRID(geom) = 0) AS srid0,
               count(*) FILTER (WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom) AND ST_SRID(geom) <> 4326
                                  AND ST_SRID(geom) <> 0) AS srid_other
        FROM %s
    """ % table)
    return cur.fetchone()


def merge_table(conn, table):
    """幂等：props->>'shape_key' 已存在的跳过；几何为空 / 非 4326 的不合并（已在统计中报告）"""
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO features (kind, name, props, geom)
        SELECT s.kind, s.name,
               (COALESCE(s.props_json::jsonb, '{}'::jsonb) - 'shape_key' - 'src')
                 || jsonb_build_object('shape_key', s.shape_key, 'src', 'shapefile'),
               ST_SetSRID(ST_Force2D(s.geom), 4326)
        FROM %s s
        WHERE s.geom IS NOT NULL
          AND NOT ST_IsEmpty(s.geom)
          AND ST_SRID(s.geom) = 4326
          AND NOT EXISTS (
              SELECT 1 FROM features f WHERE f.props->>'shape_key' = s.shape_key
          )
    """ % table)
    return cur.rowcount


def main():
    url = os.environ.get("DATABASE_URL", "")
    print("import_shapes.py — Shapefile 入库（PostGIS）")
    if not url.startswith("postgres"):
        print("[FAIL] DATABASE_URL 未设置或不以 postgres 开头（当前值: %r）" % url)
        print("       本脚本只服务 PostGIS 场景；SQLite 默认路径不经过这里。")
        return 2
    print("DATABASE_URL: %s" % redact_dsn(url))

    ogr, env, version = find_ogr2ogr()
    if not ogr:
        print("[FAIL] 未找到可用的 ogr2ogr（尝试过）：")
        print("       %s" % os.path.join(PG_BIN, "ogr2ogr.exe"))
        print("       %s" % os.path.join(BUNDLE, "bin", "ogr2ogr.exe"))
        print("       也没有 osgeo Python 绑定；请先安装 GDAL/PostGIS bundle 再重试。")
        return 1
    print("ogr2ogr     : %s" % ogr)
    print("版本        : %s" % version)
    print("GDAL_DATA   : %s" % env.get("GDAL_DATA", "<未设置>"))
    print("PROJ_LIB    : %s" % env.get("PROJ_LIB", "<未设置>"))

    import db
    conn = None
    try:
        db.pg_init_db()
        import psycopg2
        conn = psycopg2.connect(url)
        conn.autocommit = True
    except Exception as e:
        print("\n[FAIL] 数据库准备失败")
        print(db.pg_error_message(e, url))
        return 1

    tmpdir = tempfile.mkdtemp(prefix="p5shapes_")
    try:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM features")
        before = cur.fetchone()[0]
        cur.execute("DROP SCHEMA IF EXISTS %s CASCADE" % STAGING)
        cur.execute("CREATE SCHEMA %s" % STAGING)
        print("\nfeatures 导入前: %d 条" % before)

        parts, split_stats, skipped, total = split_source()
        print("\n[1] 拆分（源: data/site.geojson，共 %d 个 feature）" % total)
        for cls in TABLE:
            print("    %-7s: %d 条" % (cls, len(parts[cls])))
        if skipped:
            print("    跳过: %s" % ", ".join("#%d(%s,%s)" % s for s in skipped))
        else:
            print("    跳过: 无（空几何 0，未知类型 0）")

        print("\n[2] Shapefile 产物（%s）" % SHP_DIR)
        produced = write_shapefiles(parts, tmpdir, ogr, env)
        for cls, files in produced.items():
            if files:
                print("    %s: %s" % (cls, ", ".join("%s(%dB)" % f for f in files)))
            else:
                print("    %s: 无要素，未生成" % cls)

        print("\n[3] ogr2ogr → staging（PGDUMP，GDAL 无 PostgreSQL 驱动）")
        for cls, table in TABLE.items():
            if not parts[cls]:
                continue
            shp = os.path.join(SHP_DIR, cls + ".shp")
            sql = os.path.join(tmpdir, cls + ".sql")
            pgdump_sql(ogr, env, shp, sql, table)
            run_sql_file(conn, sql)
            total, null_geom, empty_geom, srid0, srid_other = staging_stats(
                conn, STAGING + "." + table)
            print("    %-16s 行数=%-3d 空几何=%-2d 空对象=%-2d SRID=0:%-2d SRID≠4326:%d"
                  % (STAGING + "." + table, total, null_geom, empty_geom, srid0, srid_other))

        print("\n[4] 合并进 features（按 props->>'shape_key' 幂等）")
        inserted_total = 0
        for cls, table in TABLE.items():
            if not parts[cls]:
                continue
            total, null_geom, empty_geom, srid0, srid_other = staging_stats(
                conn, STAGING + "." + table)
            inserted = merge_table(conn, STAGING + "." + table)
            skipped_n = total - null_geom - empty_geom - srid0 - srid_other - inserted
            inserted_total += inserted
            print("    %-16s 新插入=%-3d 已存在跳过=%-3d（空/坐标系异常不合并）"
                  % (table, inserted, max(skipped_n, 0)))

        cur.execute("SELECT count(*) FROM features")
        after = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM features WHERE NOT (props ? 'src')")
        seeds = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM features WHERE props->>'src' = 'shapefile'")
        imported = cur.fetchone()[0]
        cur.execute("""
            SELECT ST_GeometryType(geom) AS gtype, count(*)
            FROM features WHERE props->>'src' = 'shapefile'
            GROUP BY 1 ORDER BY 1
        """)
        mix = cur.fetchall()

        print("\n[5] 结果")
        print("    features 导入前=%d 本次新增=%d 导入后=%d（种子 %d 条 + shapefile %d 条）"
              % (before, inserted_total, after, seeds, imported))
        print("    导入记录几何类型: %s" % ", ".join("%s=%d" % (g, n) for g, n in mix))

        cur.execute("""
            SELECT id, kind, name, props, ST_GeometryType(geom) AS gtype,
                   ST_SRID(geom) AS srid, left(ST_AsText(geom), 60) AS wkt
            FROM features WHERE props->>'src' = 'shapefile' ORDER BY id LIMIT 1
        """)
        row = cur.fetchone()
        if row:
            print("    抽样: id=%s kind=%s name=%s gtype=%s srid=%s" % (row[0], row[1], row[2], row[4], row[5]))
            print("          props=%s" % json.dumps(row[3], ensure_ascii=False))
            print("          wkt=%s" % row[6])

        print("\n[6] 清理 staging")
        cur.execute("DROP SCHEMA IF EXISTS %s CASCADE" % STAGING)
        print("    staging 已删除")
        print("\n结论: Shapefile → staging → features 链路完成（重复运行不会新增重复行）")
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        if conn is not None:
            try:
                c = conn.cursor()
                c.execute("DROP SCHEMA IF EXISTS %s CASCADE" % STAGING)
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
