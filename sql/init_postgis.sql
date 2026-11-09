-- PostGIS 初始化（docker compose 首次启动时自动执行）
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS features (
    id BIGSERIAL PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT,
    props JSONB DEFAULT '{}'::jsonb,
    geom GEOMETRY(GEOMETRY, 4326) NOT NULL,
    status TEXT DEFAULT 'active',
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- 空间索引：要素查询与 MVT 出图都走它
CREATE INDEX IF NOT EXISTS idx_features_geom ON features USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_features_kind ON features (kind);

-- 示例数据由应用启动时的 db.pg_init_db() 从 data/site.geojson 导入（9 条），
-- 这里不再单独 INSERT，避免 Docker 路径与 SQLite 路径的演示数据数量不一致。
