# -*- coding: utf-8 -*-
"""
db.py — 要素库访问层

默认 SQLite（零部署，面试演示够用）；设置 DATABASE_URL=postgresql://... 时切 PostGIS。
SQLite 用 minx/miny/maxx/maxy 四列做 bbox 过滤（等价于 PostGIS 的 GiST 索引思路）。
"""
import json
import os
import sqlite3
from datetime import datetime

DB_PATH = os.environ.get("SQLITE_PATH", os.path.join(os.path.dirname(__file__), "data", "map.db"))
DATABASE_URL = os.environ.get("DATABASE_URL", "")
SEED_PATH = os.path.join(os.path.dirname(__file__), "data", "site.geojson")


# ---------------- 通用工具 ----------------
def geometry_bbox(geom):
    """返回 (minx, miny, maxx, maxy)；支持 Point/LineString/Polygon"""
    coords = geom.get("coordinates")
    if not coords:
        raise ValueError("geometry.coordinates 不能为空")

    def flat(c):
        if isinstance(c[0], (int, float)):
            yield c
        else:
            for item in c:
                yield from flat(item)

    pts = list(flat(coords))
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------- SQLite 实现 ----------------
def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _conn()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS features (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            name TEXT,
            props TEXT DEFAULT '{}',
            geometry TEXT NOT NULL,
            minx REAL, miny REAL, maxx REAL, maxy REAL,
            status TEXT DEFAULT 'active',
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT, updated_at TEXT
        )"""
    )
    # 4 列联合索引，按 bbox 过滤；PostGIS 侧对应 GiST(geometry)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_features_bbox ON features(minx, miny, maxx, maxy)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_features_kind ON features(kind)")
    # 老库迁移：已有行 ALTER 后默认 1
    cols = [row[1] for row in conn.execute("PRAGMA table_info(features)")]
    if "version" not in cols:
        conn.execute("ALTER TABLE features ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
    conn.commit()

    cur = conn.execute("SELECT COUNT(*) AS n FROM features")
    if cur.fetchone()["n"] == 0 and os.path.exists(SEED_PATH):
        with open(SEED_PATH, encoding="utf-8") as f:
            fc = json.load(f)
        for feat in fc.get("features", []):
            props = feat.get("properties", {})
            insert_feature(
                kind=props.get("kind", "unknown"),
                name=props.get("name", ""),
                props=props,
                geom=feat["geometry"],
                conn=conn,
                commit=False,
            )
        conn.commit()
    conn.close()


def insert_feature(kind, name, props, geom, conn=None, commit=True):
    own = conn is None
    if own:
        conn = _conn()
    minx, miny, maxx, maxy = geometry_bbox(geom)
    now = _now()
    cur = conn.execute(
        """INSERT INTO features (kind, name, props, geometry, minx, miny, maxx, maxy, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (kind, name, json.dumps(props, ensure_ascii=False), json.dumps(geom),
         minx, miny, maxx, maxy, now, now),
    )
    if commit:
        conn.commit()
    new_id = cur.lastrowid
    if own:
        conn.close()
    return new_id


def get_feature(feature_id):
    conn = _conn()
    row = conn.execute("SELECT * FROM features WHERE id=? AND status='active'",
                       (feature_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_feature(feature_id, version, kind, name, props, geom):
    """乐观锁：只有 id + version 同时匹配才更新；返回新 version，0 行返回 None"""
    minx, miny, maxx, maxy = geometry_bbox(geom)
    now = _now()
    conn = _conn()
    cur = conn.execute(
        """UPDATE features
           SET kind=?, name=?, props=?, geometry=?,
               minx=?, miny=?, maxx=?, maxy=?, version=version+1, updated_at=?
           WHERE id=? AND version=? AND status='active'""",
        (kind, name, json.dumps(props, ensure_ascii=False), json.dumps(geom),
         minx, miny, maxx, maxy, now, feature_id, version),
    )
    conn.commit()
    if cur.rowcount == 0:
        conn.close()
        return None
    row = conn.execute("SELECT version FROM features WHERE id=?", (feature_id,)).fetchone()
    conn.close()
    return row["version"]


def list_features(bbox=None, kind=None):
    conn = _conn()
    sql = "SELECT * FROM features WHERE status='active'"
    args = []
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    if bbox:  # bbox = (minx, miny, maxx, maxy)
        sql += " AND NOT (maxx < ? OR minx > ? OR maxy < ? OR miny > ?)"
        args += [bbox[0], bbox[2], bbox[1], bbox[3]]
    rows = conn.execute(sql + " ORDER BY id", args).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def to_geojson(rows):
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "id": r["id"],
            "version": r["version"],
            "properties": {**json.loads(r["props"] or "{}"), "kind": r["kind"],
                           "name": r["name"], "status": r["status"],
                           "updated_at": r["updated_at"]},
            "geometry": json.loads(r["geometry"]),
        } for r in rows],
    }


# ---------------- PostGIS 实现（可选，需 docker compose） ----------------
def _dsn_info(dsn):
    """用 urllib.parse 拆 DSN，只取非敏感字段；密码不进入任何输出"""
    from urllib.parse import unquote, urlsplit

    info = {"scheme": "", "host": None, "port": None, "dbname": None, "user": None, "issue": None}
    try:
        p = urlsplit(dsn)
    except ValueError as e:
        info["issue"] = "DSN 无法解析：%s" % (str(e).strip() or "<empty>")
        return info
    info["scheme"] = p.scheme
    if p.scheme not in ("postgresql", "postgres"):
        info["issue"] = "协议前缀应为 postgresql:// 或 postgres://，当前为 %r" % p.scheme
    info["host"] = p.hostname
    try:
        info["port"] = p.port
    except ValueError:
        info["issue"] = "端口无法解析（DSN 结构可能被密码中的特殊字符破坏）"
    info["dbname"] = unquote(p.path.lstrip("/")) or None
    info["user"] = unquote(p.username) if p.username else None
    return info


def _exc_details(exc):
    """message 为空时，把 psycopg2 仍可能携带的字段摊开，避免只剩 <empty>"""
    bits = []
    pgcode = getattr(exc, "pgcode", None)
    if pgcode:
        bits.append("pgcode=%s" % pgcode)
    diag = getattr(exc, "diag", None)
    if diag is not None:
        for attr in ("sqlstate", "message_primary", "message_detail", "message_hint"):
            val = getattr(diag, attr, None)
            if val:
                bits.append("%s=%s" % (attr, val))
    if getattr(exc, "pgerror", None):
        bits.append("pgerror=%s" % exc.pgerror)
    if getattr(exc, "args", None):
        bits.append("args=%r" % (exc.args,))
    return "；".join(bits) if bits else "psycopg2 未提供任何错误文本（pgcode/diag/pgerror/args 均为空）"


def pg_error_message(exc, dsn=None):
    """把 PostGIS 异常翻译成可操作的诊断文本（不含密码）；安装类问题不与 DSN 问题混谈"""
    dsn = DATABASE_URL if dsn is None else dsn
    info = _dsn_info(dsn)
    raw_text = str(exc).strip()
    raw = raw_text or "<empty>"
    name = type(exc).__name__
    low = raw.lower()
    dsn_line = ("  DSN 解析   : scheme=%s host=%s port=%s dbname=%s user=%s（密码已隐藏）"
                % (info["scheme"], info["host"], info["port"], info["dbname"], info["user"]))
    err_line = "  底层异常   : %s: %s" % (name, raw)
    detail_line = "  附加诊断   : %s" % _exc_details(exc)

    if "could not open extension control file" in low:
        return "\n".join([
            "[PostGIS 文件未部署] 扩展控制文件缺失，这是安装问题，不是 DSN/密码问题。",
            dsn_line,
            err_line,
            "  缺失位置   : C:\\Program Files\\PostgreSQL\\17\\share\\extension\\postgis.control",
            "  处理       : 确认 PostGIS 3.6 的 bundle 已合并到 PostgreSQL 17 安装目录；",
            "               share\\extension 下应有 postgis.control，lib 下应有 postgis-3.dll",
        ])
    if name == "UndefinedFunction" and "postgis_version" in low:
        return "\n".join([
            "[PostGIS 扩展未安装] 连接成功，但当前库缺少 postgis 扩展（postgis_version() 不存在）。",
            dsn_line,
            err_line,
            "  处理       : 在目标库执行 CREATE EXTENSION postgis;（需超级用户或建库用户权限）",
            "              若报 could not open extension control file，则是 PostGIS 文件未部署到 PG 安装目录，",
            "              检查 C:\\Program Files\\PostgreSQL\\17\\share\\extension",
        ])
    if "permission denied to create extension" in low:
        return "\n".join([
            "[PostGIS 扩展无权限创建] 数据库连接正常，但当前用户无权安装 postgis 扩展。",
            dsn_line,
            err_line,
            "  处理       : 用超级用户 postgres 执行 CREATE EXTENSION postgis;，或让 DBA 授权后重试",
        ])

    if not raw_text:
        # 本机实测：psycopg2 连接阶段被服务器拒绝时，message/pgcode/diag 全为空；
        # 而服务未启动/端口错会返回 Connection refused，未提供密码会返回 fe_sendauth，均非空。
        return "\n".join([
            "[PostGIS 连接被服务器拒绝] 已连上 host:port，但认证或库名被 PostgreSQL 拒绝。",
            dsn_line,
            err_line,
            detail_line,
            "  判据       : 空消息只对应服务器侧拒绝（密码错误 / 库不存在）；",
            "               服务未启动或端口错 → Connection refused；未提供密码 → fe_sendauth，均有文本",
            "  最常见     : DATABASE_URL 里的密码不是 mapdemo 的真实密码（例如仍是占位符「你的密码」）",
            "  一步定位   : $env:PGPASSWORD='<你 DSN 里的密码>'; & 'C:\\Program Files\\PostgreSQL\\17\\bin\\psql.exe' -U postgres -h 127.0.0.1 -d mapdemo -tAc 'SELECT 1'",
            "               psql 会直接给出 password authentication failed 或 database ... does not exist",
            "  注意       : 密码含 @ # / : ? % 时先 URL 编码，例如 [uri]::EscapeDataString($pwd)",
            "  psql 全路径: C:\\Program Files\\PostgreSQL\\17\\bin\\psql.exe",
        ])

    lines = [
        "[PostGIS 连接失败] 数据库连不上，服务不会回退到 SQLite。",
        dsn_line,
        err_line,
        detail_line,
    ]
    if info["issue"]:
        lines.append("  DSN 问题   : %s" % info["issue"])
    lines += [
        "  常见原因   :",
        "    1) 密码错误或未提供：核对密码；密码含 @ # / : ? % 等特殊字符时必须先 URL 编码，",
        "       例如 urllib.parse.quote(密码, safe='')",
        "    2) 库不存在：用 psql -U postgres -lqt 确认 mapdemo 是否存在",
        "    3) 服务未启动：确认 Windows 服务 postgresql-x64-17 正在运行、5432 端口在监听",
        "    4) 认证方式：pg_hba.conf 为 scram-sha-256 时，密码须与初始化时一致",
        "  psql 全路径: C:\\Program Files\\PostgreSQL\\17\\bin\\psql.exe",
    ]
    return "\n".join(lines)


def _pg():
    import psycopg2
    import psycopg2.extras
    try:
        return psycopg2.connect(DATABASE_URL)
    except psycopg2.OperationalError as e:
        # 显式配置了 PostGIS 就不静默降级；异常消息为空时给 <empty> 占位
        raise RuntimeError(pg_error_message(e)) from None


def pg_init_db():
    """PostGIS 分支建表 + 首次导入种子（与 sqlite init_db 行为对齐）"""
    conn = _pg()
    cur = conn.cursor()
    try:
        cur.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    except Exception as e:
        # 扩展安装失败单独报错（控制文件缺失/无权限），不混成连接失败
        conn.close()
        raise RuntimeError(pg_error_message(e)) from None
    cur.execute(
        """CREATE TABLE IF NOT EXISTS features (
            id BIGSERIAL PRIMARY KEY,
            kind TEXT NOT NULL,
            name TEXT,
            props JSONB DEFAULT '{}'::jsonb,
            geom GEOMETRY(GEOMETRY, 4326) NOT NULL,
            status TEXT DEFAULT 'active',
            version INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now()
        )"""
    )
    # 老库迁移：已有行 ALTER 后默认 1
    cur.execute("ALTER TABLE features ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_features_geom ON features USING GIST (geom)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_features_kind ON features (kind)")
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM features")
    if cur.fetchone()[0] == 0 and os.path.exists(SEED_PATH):
        with open(SEED_PATH, encoding="utf-8") as f:
            fc = json.load(f)
        for feat in fc.get("features", []):
            props = feat.get("properties", {})
            pg_insert_feature(
                kind=props.get("kind", "unknown"),
                name=props.get("name", ""),
                props=props,
                geom=feat["geometry"],
            )
    conn.close()


def pg_list_features(bbox=None, kind=None):
    conn = _pg()
    cur = conn.cursor(cursor_factory=__import__("psycopg2").extras.RealDictCursor)
    cond, args = ["status='active'"], []
    if kind:
        cond.append("kind=%s")
        args.append(kind)
    if bbox:
        cond.append("geom && ST_MakeEnvelope(%s,%s,%s,%s,4326)")
        args += [bbox[0], bbox[1], bbox[2], bbox[3]]
    cur.execute(
        "SELECT id, kind, name, props, ST_AsGeoJSON(geom) AS geojson, status, version, updated_at "
        "FROM features WHERE " + " AND ".join(cond) + " ORDER BY id", args)
    rows = cur.fetchall()
    conn.close()
    for r in rows:
        r["geometry"] = json.loads(r.pop("geojson"))
    return rows


def pg_insert_feature(kind, name, props, geom):
    conn = _pg()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO features (kind, name, props, geom) "
        "VALUES (%s,%s,%s,ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)) RETURNING id",
        (kind, name, json.dumps(props, ensure_ascii=False), json.dumps(geom)),
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    conn.close()
    return new_id


def pg_get_feature(feature_id):
    conn = _pg()
    cur = conn.cursor(cursor_factory=__import__("psycopg2").extras.RealDictCursor)
    cur.execute(
        "SELECT id, kind, name, props, ST_AsGeoJSON(geom) AS geojson, status, version, updated_at "
        "FROM features WHERE id=%s AND status='active'", (feature_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    row["geometry"] = json.loads(row.pop("geojson"))
    return row


def pg_update_feature(feature_id, version, kind, name, props, geom):
    """乐观锁：UPDATE ... WHERE id + version；返回新 version，0 行返回 None"""
    conn = _pg()
    cur = conn.cursor()
    cur.execute(
        "UPDATE features SET kind=%s, name=%s, props=%s, "
        "geom=ST_SetSRID(ST_GeomFromGeoJSON(%s),4326), version=version+1, updated_at=now() "
        "WHERE id=%s AND version=%s AND status='active' RETURNING version",
        (kind, name, json.dumps(props, ensure_ascii=False), json.dumps(geom),
         feature_id, version),
    )
    row = cur.fetchone()
    conn.commit()
    conn.close()
    return row[0] if row else None


def pg_tile_mvt(z, x, y):
    """PostGIS 原生瓦片：ST_TileEnvelope + ST_AsMVT（生产路径）"""
    conn = _pg()
    cur = conn.cursor()
    cur.execute(
        """
        WITH bounds AS (SELECT ST_TileEnvelope(%s, %s, %s) AS geom),
        mvtgeom AS (
            SELECT f.id, f.kind, f.name, f.props,
                   ST_AsMVTGeom(ST_Transform(f.geom, 3857), b.geom, 4096, 64, true) AS geom
            FROM features f, bounds b
            WHERE f.status = 'active'
              AND ST_Transform(f.geom, 3857) && b.geom
        )
        SELECT ST_AsMVT(mvtgeom, 'site', 4096, 'geom', 'id') FROM mvtgeom
        """,
        (z, x, y),
    )
    row = cur.fetchone()
    conn.close()
    # psycopg2 的 bytea 返回 memoryview，Starlette Response 只接受 bytes
    return bytes(row[0]) if row[0] is not None else b""
