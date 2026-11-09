# Shapefile 入库逐步讲解（项目⑤）

> 目标：你能亲手跑一遍，也能不看稿把这条链路讲清楚。
> 实测环境：Windows 10/11 + PostgreSQL 17 + PostGIS 3.6 + GDAL 3.9.2；实测日期 2026-11-07。
> 实测数据：`data/site.geojson` 共 9 个 feature（Point 4 / LineString 2 / Polygon 3）。
> 实测结果：拆分 4+2+3 → staging 4+2+3 → 合并新增 9 → `features` 9→18；第二次运行新增 0。
> 所有命令里的密码一律写成 `你的密码` 占位符。

---

## 1. 整体流程图

```text
┌──────────────────────────── Python 进程（tools/import_shapes.py）────────────────────────────┐
│                                                                                              │
│  [输入] data/site.geojson（9 个 feature，点/线/面混合）                                       │
│      │                                                                                       │
│      │ ① split_source()：按 geometry.type 分成 3 组，补 shape_key / props_json                │
│      ▼                                                                                       │
│  内存中的 3 个 FeatureCollection（point 4 / line 2 / polygon 3）                              │
│      │                                                                                       │
│      │ ② write_shapefiles()：每组写临时 GeoJSON，再调用 ogr2ogr                              │
│      ▼                                                                                       │
│  [输出] data/site/shapefile/{point,line,polygon}.{shp,shx,dbf,prj,cpg}                        │
│                                                                                              │
└───────────────────────────────────────┬──────────────────────────────────────────────────────┘
                                        │ ③ 调用 ogr2ogr CLI（子进程）
                                        ▼
┌──────────────────────────── ogr2ogr CLI（GDAL 3.9.2）────────────────────────────────────────┐
│  读取 .shp + .dbf + .prj                                                                     │
│  [输出] %TEMP%\p5shapes_xxx\{point,line,polygon}.sql（PGDUMP 文本）                           │
│  注意：本机 GDAL 没有 PostgreSQL 驱动，所以不能 -f PostgreSQL 直连                             │
└───────────────────────────────────────┬──────────────────────────────────────────────────────┘
                                        │ ④ Python 用 psycopg2 执行 SQL（先过滤 CREATE SCHEMA）
                                        ▼
┌──────────────────────────── PostgreSQL / PostGIS 进程 ────────────────────────────────────────┐
│  [中间表] staging.site_point(4) / staging.site_line(2) / staging.site_polygon(3)              │
│      │                                                                                       │
│      │ ⑤ 统计空几何 / ST_SRID                                                               │
│      │ ⑥ INSERT ... SELECT ... WHERE NOT EXISTS(shape_key)                                 │
│      ▼                                                                                       │
│  [正式表] features（9 条种子 + 9 条导入 = 18 条）                                             │
│      │                                                                                       │
│      │ ⑦ DROP SCHEMA staging CASCADE                                                         │
│      ▼                                                                                       │
│  [结束状态] features=18，staging 不存在，临时目录已删除                                       │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

一句话版本：**Python 负责"拆"和"合并"，ogr2ogr 负责"读 Shapefile 并生成 SQL"，PostgreSQL 负责"落 staging 和正式表"**。

---

## 2. 每一步拆开讲

### 步骤 0：确认工具与环境变量

**做什么**：找到可运行的 `ogr2ogr`，并给它补上 `GDAL_DATA`、`PROJ_LIB`、`PATH`。

**为什么必须这么做**：
本机 `ogr2ogr.exe` 有两份，但都不在 PATH：

- `C:\Program Files\PostgreSQL\17\bin\ogr2ogr.exe`
- `F:\001download\001application\postgis-bundle-pg17-3.6.2x64\postgis-bundle-pg17-3.6.2x64\bin\ogr2ogr.exe`

第一次直接运行时进程起不来，退出码就是这次真实遇到的：

```text
0xC0000135（-1073741515）：Windows 提示“应用程序无法正常启动”/“找不到指定的模块”
```

原因是 GDAL 的 DLL、投影数据库、数据字典不在默认搜索路径。只设 `GDAL_DATA` 不够，必须把两份 `bin` 目录也加进 `PATH`。

**实际执行的命令**（PowerShell，可复制）：

```powershell
$pgbin  = "C:\Program Files\PostgreSQL\17\bin"
$bundle = "F:\001download\001application\postgis-bundle-pg17-3.6.2x64\postgis-bundle-pg17-3.6.2x64"

$env:PATH      = "$bundle\bin;$pgbin;" + $env:PATH
$env:GDAL_DATA = "$bundle\gdal-data"
$env:PROJ_LIB  = "$bundle\share\contrib\postgis-3.6\proj"

& "$pgbin\ogr2ogr.exe" --version
& "$pgbin\ogrinfo.exe" --formats | Select-String "PGDUMP|PostgreSQL"
```

`tools/import_shapes.py` 里的 `tool_env()` / `find_ogr2ogr()` 就是自动做这件事；可用环境变量 `PGBIN`、`POSTGIS_BUNDLE` 覆盖路径。

**怎么验证这一步成功了**：

```text
GDAL 3.9.2, released 2024/08/13
  PGDUMP -raster,vector- (w+): PostgreSQL Dump
```

看到版本号 + `PGDUMP`，且列表里**没有** `PostgreSQL`，就说明工具可用，同时也说明"直连数据库"这条路在本机走不通。

---

### 步骤 1：按几何类型拆分 GeoJSON

**做什么**：把混合几何的 9 个 feature 拆成 point / line / polygon 三组，并给每条补 `shape_key`、`props_json`。

**为什么必须这么做**：
一个 Shapefile 的 `.shp` **只能存一种几何类型**。把 Point 和 Polygon 塞进同一个 `.shp`，ogr2ogr 会在写入时报几何类型冲突或在读取时丢要素。因为源数据是混合的，所以必须先拆。

另外两个字段是后面两步的关键：

- `shape_key`：如 `sf-point-04`，跨运行稳定的幂等键。
- `props_json`：把原始属性序列化成一个字段，抵消 DBF 的字段名/宽度限制。

**实际执行的命令**：

先看源数据几何分布（只读，不改文件）：

```powershell
cd project5_map_platform
.\.venv\Scripts\python.exe -c "import json,collections; fc=json.load(open(r'data\site.geojson',encoding='utf-8')); print(len(fc['features']), collections.Counter(f['geometry']['type'] for f in fc['features']))"
```

整条链路用一条命令跑：

```powershell
$env:DATABASE_URL = "postgresql://postgres:你的密码@127.0.0.1:5432/mapdemo"
.\.venv\Scripts\python.exe tools\import_shapes.py
```

拆分逻辑在 `split_source()`：空 `coordinates` 或未知类型不会被静默吞掉，会进入 `stats` 并打印 `跳过: #idx(type,原因)`。

**怎么验证这一步成功了**：

源数据统计应为：

```text
9 Counter({'Point': 4, 'Polygon': 3, 'LineString': 2})
```

脚本 `[1]` 段应打印：

```text
[1] 拆分（源: data/site.geojson，共 9 个 feature）
    point  : 4 条
    line   : 2 条
    polygon: 3 条
    跳过: 无（空几何 0，未知类型 0）
```

---

### 步骤 2：写出三套 Shapefile（五个文件各管什么）

**做什么**：每组生成一套 `.shp/.shx/.dbf/.prj`，并额外写 `.cpg`。

**为什么必须这么做**：

| 文件 | 作用 | 缺了会怎样 |
|---|---|---|
| `.shp` | 存几何坐标（点/线/面） | 没有几何，整个数据不存在 |
| `.shx` | 几何记录的空间索引（定长偏移） | 部分软件仍能重建，但有的读取器直接报"不是有效 Shapefile" |
| `.dbf` | 存属性表（字段名、类型、值） | 只剩几何，`kind/name` 全丢 |
| `.prj` | 声明坐标系（这里是 `GCS_WGS_1984`） | 读入后 `ST_SRID=0`，PostGIS 不知道坐标含义；本脚本直接报错 |
| `.cpg` | 声明 DBF 的编码（这里是 `UTF-8`） | 中文可能按 ANSI/GBK 解码成乱码 |

注意 `.prj` 是**我们生成时写死的 WGS84**，不是从源数据继承——GeoJSON 按 RFC 7946 本身就是 WGS84 经纬度。

**实际执行的命令**（脚本内部对每类执行，等价手动命令以 point 为例）：

```powershell
$ogr = "C:\Program Files\PostgreSQL\17\bin\ogr2ogr.exe"
& $ogr -f "ESRI Shapefile" -lco ENCODING=UTF-8 `
  "data\site\shapefile\point.shp" "$env:TEMP\point.geojson" `
  -nln point -nlt POINT
```

- `-f "ESRI Shapefile"`：输出格式。
- `-lco ENCODING=UTF-8`：DBF 用 UTF-8，并生成 `.cpg`。
- `-nlt POINT`：强制点类型；如果该组含 Multi 几何，脚本改用 `PROMOTE_TO_MULTI`。

**怎么验证这一步成功了**：

```powershell
Get-ChildItem data\site\shapefile | Select-Object Name,Length
& $ogr -so -al "data\site\shapefile\point.shp"
```

本机实测产物（字节数）：

| 类型 | .shp | .shx | .dbf | .prj | .cpg |
|---|---|---|---|---|---|
| point | 212 | 132 | 1582 | 145 | 5 |
| line | 292 | 116 | 992 | 145 | 5 |
| polygon | 508 | 124 | 1456 | 145 | 5 |

`ogrinfo -so -al` 应看到 `Feature Count: 4`、`Geometry: Point`、`Layer SRS WKT: GEOGCS["GCS_WGS_1984"...]`。
`.prj` 内容应为 `GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",...]]`，`.cpg` 内容为 `UTF-8`。

---

### 步骤 3：ogr2ogr 生成 PGDUMP SQL（而不是直连数据库）

**做什么**：把每套 Shapefile 转成一份 PostgreSQL 可执行的 SQL 文本。

**为什么必须这么做**：
常规写法是 `ogr2ogr -f PostgreSQL "PG:..."` 直接入库，但本机这次实测 `ogrinfo --formats` **只有 `PGDUMP`，没有 `PostgreSQL`**——这份 GDAL 没编译 PostgreSQL 驱动。硬写 `-f PostgreSQL` 会直接报驱动不存在。

改成 `-f PGDUMP` 后，ogr2ogr 仍负责"读 Shapefile + 生成建表/插入 SQL"，只是把"执行"这一步交给 Python 的 psycopg2。这样不需要额外装驱动，也不引入新依赖。

**实际执行的命令**：

```powershell
& $ogr -f PGDUMP "$env:TEMP\point.sql" "data\site\shapefile\point.shp" `
  -lco SCHEMA=staging -lco GEOMETRY_NAME=geom -lco FID=ogc_fid -nln site_point
```

参数含义：

- `-lco SCHEMA=staging`：SQL 里的表建在 `staging` schema。
- `-lco GEOMETRY_NAME=geom`：几何列叫 `geom`，与合并 SQL 对齐。
- `-lco FID=ogc_fid`：自增主键列叫 `ogc_fid`。
- `-nln site_point`：表名。

**怎么验证这一步成功了**：

```powershell
Get-Item "$env:TEMP\point.sql" | Select-Object Length
Select-String -Path "$env:TEMP\point.sql" -Pattern 'CREATE TABLE|CREATE SCHEMA|INSERT INTO' | Select-Object -First 5
```

应看到文件非空、包含 `CREATE SCHEMA "staging";`、`CREATE TABLE "staging"."site_point"`、`INSERT INTO "staging"."site_point"`，
且表定义里字段是 `kind`、`name`、`capacity_t...`、`shape_key`、`props_json`、`geom geometry(Point,4326)` 这类平铺结构。

---

### 步骤 4：执行 PGDUMP 进 staging 三张表

**做什么**：先建空的 `staging` schema，再把三份 SQL 依次执行，得到 `staging.site_point`、`staging.site_line`、`staging.site_polygon`。

**为什么必须这么做**：

1. **结构不同**：`features.props` 是 JSONB，而 Shapefile 的 DBF 是平铺字段。直接往 `features` 追加列名对不上。
2. **先隔离再合并**：staging 表可以随便长什么样，合并时再按 `features` 的结构做映射；出问题也不会污染正式表。
3. **可重复**：每次运行重建 staging，不残留上次的数据。

这里踩过两个真实报错：

```text
psycopg2.errors.DuplicateSchema: 模式 "staging" 已经存在
```

原因：PGDUMP 每份 SQL 都自带 `CREATE SCHEMA "staging";`（不带 `IF NOT EXISTS`），三份执行到第二份就冲突。
处理：脚本先 `DROP SCHEMA IF EXISTS staging CASCADE` + `CREATE SCHEMA staging`，执行 SQL 时过滤掉 `CREATE SCHEMA` 行。

```text
psycopg2.errors.UndefinedTable: 关系 "site_point" 不存在
```

原因：统计 SQL 当时写了 `FROM site_point`，没有 schema 前缀。
处理：改成 `FROM staging.site_point`（`staging_stats()` 调用处）。

**实际执行的命令**（手动复现；脚本里是 psycopg2 执行）：

```powershell
$psql = "C:\Program Files\PostgreSQL\17\bin\psql.exe"
$env:PGPASSWORD = "你的密码"
$env:PGCLIENTENCODING = "UTF8"

& $psql -h 127.0.0.1 -p 5432 -U postgres -d mapdemo -c "DROP SCHEMA IF EXISTS staging CASCADE; CREATE SCHEMA staging;"

# PGDUMP 自带 CREATE SCHEMA，先过滤掉，否则 DuplicateSchema
(Get-Content "$env:TEMP\point.sql") |
  Where-Object { $_ -notmatch '^CREATE SCHEMA' } |
  Set-Content "$env:TEMP\point_no_schema.sql" -Encoding UTF8

& $psql -h 127.0.0.1 -p 5432 -U postgres -d mapdemo -f "$env:TEMP\point_no_schema.sql"
```

line / polygon 同理，分别换成 `line.sql` / `site_line`、`polygon.sql` / `site_polygon`。

**怎么验证这一步成功了**：

```powershell
& $psql -h 127.0.0.1 -p 5432 -U postgres -d mapdemo -c "\dt staging.*"
& $psql -h 127.0.0.1 -p 5432 -U postgres -d mapdemo -c "SELECT count(*) FROM staging.site_point;"
```

应看到三张表 `site_point / site_line / site_polygon`，行数分别 **4 / 2 / 3**。

> 注意：`import_shapes.py` 跑完会在步骤 7 删除 staging。想亲手看 staging，请在它打印 `[3]` 时另开一个窗口查，或者按上面命令手动执行。

---

### 步骤 5：staging 质检（空几何、SRID）

**做什么**：在合并前统计每张 staging 表的总行数、空几何、空对象、`SRID=0`、`SRID≠4326`。

**为什么必须这么做**：
"能导入"不等于"数据是对的"。空几何、坐标系缺失如果直接进正式表，要么插入失败（`features.geom` 是 `NOT NULL` 且声明 4326），要么变成看不见的坏数据。先统计再合并，问题看得见、可解释。

**实际执行的命令**：

```powershell
& $psql -h 127.0.0.1 -p 5432 -U postgres -d mapdemo -c @"
SELECT count(*) AS total,
       count(*) FILTER (WHERE geom IS NULL) AS null_geom,
       count(*) FILTER (WHERE geom IS NOT NULL AND ST_IsEmpty(geom)) AS empty_geom,
       count(*) FILTER (WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom) AND ST_SRID(geom) = 0) AS srid0,
       count(*) FILTER (WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
                          AND ST_SRID(geom) <> 4326 AND ST_SRID(geom) <> 0) AS srid_other
FROM staging.site_point;
"@
```

**怎么验证这一步成功了**：

本机实测三张表全部是：

```text
total=4 / 2 / 3，null_geom=0，empty_geom=0，srid0=0，srid_other=0
```

脚本对应输出：

```text
staging.site_point  行数=4   空几何=0  空对象=0  SRID=0:0  SRID≠4326:0
staging.site_line   行数=2   空几何=0  空对象=0  SRID=0:0  SRID≠4326:0
staging.site_polygon 行数=3  空几何=0  空对象=0  SRID=0:0  SRID≠4326:0
```

如果这里出现非 0，不要继续合并；先查源数据或 `.prj`。

---

### 步骤 6：合并进 `features`（字段映射 + `shape_key` 幂等）

**做什么**：把 staging 的平铺字段映射成 `features` 的 `(kind, name, props JSONB, geom)`，并按 `shape_key` 去重。

**为什么必须这么做**：

字段映射关系：

| staging 列 | features 列 | 处理 |
|---|---|---|
| `kind` | `kind` | 直接取 |
| `name` | `name` | 直接取 |
| `props_json` | `props` | 先转 `jsonb`，去掉旧的 `shape_key/src`，再写回 `shape_key` 和 `src='shapefile'` |
| `geom` | `geom` | `ST_SetSRID(ST_Force2D(geom), 4326)`，统一二维 + 4326 |
| — | `status` | 用表默认值 `active` |
| — | `created_at/updated_at` | 用表默认 `now()` |

幂等靠 `NOT EXISTS`：

```sql
NOT EXISTS (SELECT 1 FROM features f WHERE f.props->>'shape_key' = s.shape_key)
```

也就是说，重复运行只会重建 staging，不会重复插入，也不会改已有 id。为什么不用 `ON CONFLICT`？因为 `features` 没有 `(props->>'shape_key')` 的唯一索引，而且这里要的是"不重复"，不是"覆盖同步"。

**实际执行的命令**（单表核心 SQL）：

```sql
INSERT INTO features (kind, name, props, geom)
SELECT s.kind, s.name,
       (COALESCE(s.props_json::jsonb, '{}'::jsonb) - 'shape_key' - 'src')
         || jsonb_build_object('shape_key', s.shape_key, 'src', 'shapefile'),
       ST_SetSRID(ST_Force2D(s.geom), 4326)
FROM staging.site_point s
WHERE s.geom IS NOT NULL
  AND NOT ST_IsEmpty(s.geom)
  AND ST_SRID(s.geom) = 4326
  AND NOT EXISTS (
      SELECT 1 FROM features f WHERE f.props->>'shape_key' = s.shape_key
  );
```

line / polygon 把表名和 `site_point` 换掉即可。整体链路直接运行 `import_shapes.py`。

**怎么验证这一步成功了**：

```powershell
& $psql -h 127.0.0.1 -p 5432 -U postgres -d mapdemo -c "SELECT count(*) FROM features;"
& $psql -h 127.0.0.1 -p 5432 -U postgres -d mapdemo -c "SELECT count(*) FROM features WHERE props->>'src'='shapefile';"
& $psql -h 127.0.0.1 -p 5432 -U postgres -d mapdemo -c "SELECT props->>'shape_key' AS k, count(*) FROM features WHERE props->>'src'='shapefile' GROUP BY 1 HAVING count(*)>1;"
```

预期：总数 **18**；导入 9；重复 shape_key 查询**无行**。
再跑第二次 `import_shapes.py`，预期 `本次新增=0`、总数仍 18。

---

### 步骤 7：清理 staging 与临时目录

**做什么**：删除 `staging` schema 和 `%TEMP%\p5shapes_xxx` 临时目录。

**为什么必须这么做**：
staging 是中间态，留着会让下次运行的 `CREATE SCHEMA` 冲突，也容易被误当成正式表。临时 SQL/GeoJSON 留着会占空间、也可能包含数据副本。脚本在正常结束和 `finally` 里各删一次，失败中断也不会残留。

**实际执行的命令**：

```powershell
& $psql -h 127.0.0.1 -p 5432 -U postgres -d mapdemo -c "DROP SCHEMA IF EXISTS staging CASCADE;"
Remove-Item "$env:TEMP\p5shapes_*" -Recurse -Force -ErrorAction SilentlyContinue
```

**怎么验证这一步成功了**：

```powershell
& $psql -h 127.0.0.1 -p 5432 -U postgres -d mapdemo -c "SELECT count(*) FROM information_schema.schemata WHERE schema_name='staging';"
```

预期 `0`；`Get-ChildItem $env:TEMP -Filter p5shapes_*` 无结果。

---

## 3. 每一步都能用 psql 单独验证（命令与预期）

先统一连接方式：

```powershell
$psql = "C:\Program Files\PostgreSQL\17\bin\psql.exe"
$env:PGPASSWORD = "你的密码"
$env:PGCLIENTENCODING = "UTF8"
& $psql -h 127.0.0.1 -p 5432 -U postgres -d mapdemo
```

| 查什么 | 命令 | 预期 |
|---|---|---|
| 有哪些表 | `\dt` | `features`（启用 PostGIS 后还会有 `spatial_ref_sys`）；运行中会出现 `staging.*` |
| 表结构 | `\d features` | `id bigint`、`kind text NOT NULL`、`name text`、`props jsonb`、`geom geometry(Geometry,4326) NOT NULL`、`status text DEFAULT 'active'`、两个时间列；索引 `features_pkey`、`idx_features_geom (gist)`、`idx_features_kind` |
| 总数 | `SELECT count(*) FROM features;` | 首次导入后 **18** |
| 种子数 | `SELECT count(*) FROM features WHERE NOT (props ? 'src');` | **9** |
| 导入数 | `SELECT count(*) FROM features WHERE props->>'src'='shapefile';` | **9** |
| 按类型 | `SELECT kind, count(*) FROM features GROUP BY kind ORDER BY kind;` | `crusher 2, fuel 2, geofence 4, load 2, road 4, stockpile 2, unload 2`（合计 18） |
| 几何类型 | `SELECT ST_GeometryType(geom) g, count(*) FROM features GROUP BY 1 ORDER BY 1;` | `ST_LineString 4, ST_Point 8, ST_Polygon 6` |
| SRID | `SELECT DISTINCT ST_SRID(geom) FROM features;` | 只有 `4326` |
| 抽样属性 | `SELECT id, kind, name, props FROM features WHERE props->>'shape_key'='sf-point-04';` | `id=10`，`crusher / 破碎站`，props 含 `capacity_tph: 800`、`shape_key`、`src` |
| 是否有重复键 | `SELECT props->>'shape_key' k, count(*) FROM features WHERE props->>'src'='shapefile' GROUP BY 1 HAVING count(*)>1;` | 无行 |
| staging 是否清掉 | `SELECT count(*) FROM information_schema.schemata WHERE schema_name='staging';` | `0` |

`\d features` 的实际排版会因终端宽度略有差异，字段和索引以本机 psql 输出为准。

另外用项目自带脚本验证瓦片：

```powershell
$env:DATABASE_URL = "postgresql://postgres:你的密码@127.0.0.1:5432/mapdemo"
.\.venv\Scripts\python.exe tools\verify_pg.py
```

预期 4/4 PASS，`layers=['site']`、`site_features=6`（z16/x54723/y26763，种子 3 + 导入 3），`X-Tile-Source: postgis-st_asmvt`。

---

## 4. 常见报错对照表（本次真实遇到）

| 报错原文 / 现象 | 原因 | 处理 |
|---|---|---|
| `0xC0000135`（退出码 `-1073741515`），Windows 提示“应用程序无法正常启动”或“找不到指定的模块” | `ogr2ogr.exe` 找不到 GDAL DLL / 数据文件，两份 bin 都不在 PATH | `$env:PATH = "$bundle\bin;$pgbin;" + $env:PATH`；再设 `GDAL_DATA`、`PROJ_LIB`；脚本 `tool_env()` 已自动处理 |
| `psycopg2.errors.DuplicateSchema: 模式 "staging" 已经存在` | PGDUMP 每份 SQL 都含 `CREATE SCHEMA "staging";`，三份连续执行时第二份冲突 | 先 `DROP SCHEMA IF EXISTS staging CASCADE` + `CREATE SCHEMA staging`；执行时过滤 `CREATE SCHEMA` 行 |
| `psycopg2.errors.UndefinedTable: 关系 "site_point" 不存在` | 统计 SQL 写成 `FROM site_point`，缺 schema 前缀 | 改为 `FROM staging.site_point`（脚本 `staging_stats()` 调用处已修） |
| 不是报错而是静默改名：DBF 字段 `capacity_tph` 变为 `capacity_1`；`speed_limit` 变为 `speed_limi` | DBF 字段名上限 10 字符。`capacity_tph` 截成 `capacity_t` 后与已有 `capacity_t` 重名，ogr2ogr 自动追加序号 | 不依赖 DBF 字段名做业务映射；完整属性放在 `props_json`，合并时还原成 JSONB |
| staging 统计出现 `SRID=0` 或 `SRID≠4326`（本链路不报错，走统计 + 跳过） | Shapefile 缺 `.prj`，或坐标系不是 WGS84 | 本脚本生成 `.prj` 并校验；外部数据先用 `ogr2ogr -t_srs EPSG:4326` 重投影。若绕过过滤直插，PostGIS 会报 `Geometry SRID (0) does not match column SRID (4326)`（本链路未触发） |
| `RuntimeError: 缺少 Shapefile 组件：...\point.prj` | ogr2ogr 没生成 `.prj` | 确认 `-f "ESRI Shapefile"` 正常完成；源 GeoJSON 需为 WGS84 经纬度 |
| `RuntimeError: 坐标系不是 WGS84：GEOGCS[...]` | `.prj` 内容不是 `GCS_WGS_1984` | 源数据先重投影到 EPSG:4326，再重新拆分生成 |

补充：`ogr2ogr -f PostgreSQL ...` 在本机会因缺少 PostgreSQL 驱动而失败（`ogrinfo --formats` 只有 PGDUMP）。这不是配置问题，是这份 GDAL 的编译选项，改用 PGDUMP 即可。

---

## 5. 60 秒讲法（可直接背）

> 这份数据是点、线、面混在一起的 GeoJSON，而一个 Shapefile 只能存一种几何类型，所以我先按类型拆成 point、line、polygon 三套，每套都有 shp 存几何、shx 存索引、dbf 存属性、prj 声明 WGS84、cpg 声明 UTF-8。
> 入库没有直接 append 到 features，因为 Shapefile 的 dbf 是平铺字段，features 的属性是 JSONB，结构对不上。我先用 ogr2ogr 生成 PGDUMP SQL，落到 staging 的三张表，先统计空几何和 SRID，再把平铺字段映射成 JSONB 合并进 features。
> 幂等靠 shape_key：每条导入数据带一个稳定的键，比如 sf-point-04，合并时用 NOT EXISTS 判断，已经存在就跳过。所以重复运行只会重建 staging，不会重复插入，也不会改已有 id。跑完 staging 和临时目录都会删掉。
> 实测 9 条拆成 4+2+3，合并新增 9 条，features 从 9 到 18；第二次运行新增 0。

---

## 6. 可能被追问的 5 个问题

**Q1：为什么不用 Python GDAL 绑定（osgeo），非要用 CLI？**
本机没有装 `osgeo`，Windows 上 `pip install gdal` 需要匹配本机 GDAL 版本和编译环境，容易失败；而 `ogr2ogr.exe` 已经带了完整驱动和数据目录。用 `subprocess` 调 CLI，参数就是文档，报错原文也能直接看到，排查成本低。代价是多一层进程边界，但对这个数据量没有性能问题。

**Q2：staging 会不会锁表、影响线上读取？**
staging 是独立 schema 的普通表，写入它不影响 `features`。真正碰 `features` 的只有最后一条 `INSERT ... SELECT`，在 autocommit 下是一个事务，本机 18 行规模是毫秒级。数据量大时需要评估锁时间和分批；这一点没有压测，属于未验证。

**Q3：数据是几个 G 怎么办？**
当前实现是一次性 `json.load` 进内存、一次性生成 SQL、一次性 INSERT，只适合演示/中小数据。大文件要改成流式读取、按块写 shp、分批 INSERT（例如每 5000 行），或者换成带 PostgreSQL 驱动的 GDAL 直连入库；这些都没有实现。

**Q4：为什么不直接 upsert，非要 NOT EXISTS？**
upsert 需要唯一约束，而 `features` 没有 `(props->>'shape_key')` 的唯一索引；更重要的是，这里要的是"重复导入不产生重复行"，不是"用源数据覆盖线上数据"。已有行可能被前端改过，直接覆盖会丢人工编辑。需要同步语义时，应显式加唯一索引 + `ON CONFLICT DO UPDATE`，并明确覆盖策略。

**Q5：坐标系不一致怎么处理？**
当前只接受 `SRID=4326`，不做隐式重投影。staging 统计会把 `SRID=0` 和 `SRID≠4326` 单独报出来，合并不符合条件的数据。正确做法是在导入前用 `ogr2ogr -t_srs EPSG:4326` 重投影，并保留原始坐标系记录；脚本没有默认开启重投影，是为了避免悄悄改数据。

---

## 7. 当前已知边界

- **只处理 SRID=4326**：源必须是 WGS84 经纬度；其他坐标系不自动转换，会被统计并跳过。
- **缺 `.prj` 直接报错**：不猜坐标系。本脚本自己生成 `.prj` 并校验；外部 Shapefile 缺 `.prj` 不在当前入口范围内（入口固定为 `data/site.geojson`）。
- **同 `shape_key` 只跳过、不更新**：重复导入不会覆盖已修改的行；没有属性同步能力。
- **`props_json` 有长度上限**：DBF 文本字段按该套数据的最长值自动定宽（本机实测 point/polygon 的 `props_json` 为 80 字符，line 为 92 字符）。这 9 条都能放下，未观察到截断；更长 JSON 会被静默截断，**未验证**。
- **未做分批**：全量读内存 + 单条 INSERT，只适合当前演示数据量。
- **未覆盖多图层 Shapefile**：一次只处理一套 `.shp`；多图层需要逐层拆分。
- **未覆盖非 UTF-8 的旧 DBF**：脚本生成时强制 UTF-8 并写 `.cpg`，读取外部 GBK 数据未验证。
- **导入数据与种子同源**：首次导入会在 9 条种子之外再加 9 条（总数 18），这是演示数据重复，不是种子被改。回退可删 `WHERE props->>'src'='shapefile'` 的记录。
- **只服务 PostGIS**：SQLite 默认路径不经过这条链路；`DATABASE_URL` 不以 `postgres` 开头时脚本直接退出（返回码 2）。

---

## 8. 文件与命令索引

| 文件 | 作用 |
|---|---|
| `tools/import_shapes.py` | 唯一入口：探测工具 → 拆分 → 写 Shapefile → PGDUMP → staging → 合并 → 清理 |
| `data/site.geojson` | 源数据，9 个混合几何 feature |
| `data/site/shapefile/point.*` 等 | 拆分产物，15 个文件（3 套 × 5） |
| `db.py` | `pg_init_db()` 建 `features`、GiST 索引、9 条种子 |
| `tools/verify_pg.py` | 导入后 4 项 PostGIS/瓦片断言 |

一条命令跑完整链路：

```powershell
$env:DATABASE_URL = "postgresql://postgres:你的密码@127.0.0.1:5432/mapdemo"
.\.venv\Scripts\python.exe tools\import_shapes.py
```
