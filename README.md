# 高精地图平台（Web 端）

[![CI](https://github.com/evenxu777/hdmap-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/evenxu777/hdmap-platform/actions/workflows/ci.yml)

把园区/矿区的矢量地图数据从文件导入数据库，通过 HTTP 接口提供要素查询、MVT 瓦片和在线编辑，并在浏览器里完成查看、点选与编辑的最小可用平台。

当前定位是单机运行的内部工具原型：默认无用户体系、示例数据在万级以下、按需生成瓦片。生产环境需要的权限、切片缓存、并发与运维能力没有做，详见[已知限制](#9-已知限制与未实现项)。

## 1. 项目是什么

矿区、园区一类场景的地图数据通常散落在 Shapefile、GeoJSON 和 CAD 导出文件里，缺少统一的查询、编辑和可视化入口。这个项目提供一条最小链路：

- 数据侧：GeoJSON 开箱入库；Shapefile 经 GDAL/PGDUMP 转换后幂等合并进 `features` 表。
- 服务侧：同一套 FastAPI 接口，底层可在 SQLite（默认，零配置）和 PostGIS（`DATABASE_URL`）之间切换。
- 浏览侧：OpenLayers 加载 WGS84 要素，支持点选属性、点位新增、点位编辑（`version` 乐观锁）、本地网格底图与在线 OSM 切换。
- 发布侧：提供 XYZ 的 MVT 瓦片接口和 TileJSON；PostGIS 走 `ST_AsMVT`，SQLite/无 PostGIS 时走 Python 编码。
- 三维侧：用 Three.js 把同一份 GeoJSON 拉伸成简单体块，用于确认数据形态，不做真实三维场景。

## 2. 功能范围

已实现：

| 模块 | 内容 |
| --- | --- |
| 存储 | SQLite 单文件（默认）、PostGIS（PostgreSQL 17 + PostGIS 3.6 实测） |
| 入库 | GeoJSON 首次自动导入；Shapefile 拆分为点/线/面后经 staging 合并，按 `shape_key` 幂等 |
| 查询 | 全量/按 `bbox`/按 `kind` 查询；单要素查询；总数与分类统计 |
| 瓦片 | `GET /api/tiles/{z}/{x}/{y}.pbf`；PostGIS `ST_AsMVT`，layer 名 `site`；Python 路径也可生成 MVT |
| 编辑 | 新增要素；点位更新；`X-Edit-Token` 鉴权；`version` 乐观锁，冲突返回 409 |
| 运维 | `/api/health`、`/api/metrics`（Prometheus 文本格式）、请求 ID 透传、结构化错误体 |
| 前端 | 2D 查看、属性面板、新增点位、点位编辑、2D/3D 切换、离线网格底图/在线 OSM |
| 验证 | `pytest` 接口测试；`tools/verify_pg.py`、`tools/verify_offline.py`、`tools/verify_edit.py` |

未实现：

- 前端不从 MVT 渲染，2D 页面加载的是 `/api/features` 返回的 GeoJSON；MVT 目前供外部调用方使用。
- 线、面要素的编辑与删除；要素状态切换；批量导入接口。
- 用户、角色、审计日志；CORS 固定为 `*`。
- 瓦片缓存、预切片、CDN、压缩；高并发与压力测试。
- 点云、倾斜摄影、3D Tiles、地形与真实高程；3D 只是按属性高度拉伸。
- 生产部署编排（CI 已接入 GitHub Actions，见 4.4）；仓库里的 Docker 文件未在本机实际构建验证。

## 3. 技术栈与数据流

- 后端：Python 3.10、FastAPI 0.111、Uvicorn 0.30
- 数据库：SQLite（标准库 `sqlite3`）或 PostgreSQL 17 + PostGIS 3.6（`psycopg2-binary`）
- 瓦片：PostGIS `ST_TileEnvelope` + `ST_AsMVT`；Python `mapbox-vector-tile`
- 前端：OpenLayers 7.5.2、Three.js r134，均放在 `static/vendor/`，不依赖 CDN
- 测试：pytest + httpx

数据流一句话：**GeoJSON/Shapefile → `features` 表（SQLite 或 PostGIS）→ `/api/features` 或 `/api/tiles` → OpenLayers / Three.js**。

```
data/site.geojson ──init_db()──▶ features(SQLite) / features(PostGIS)
Shapefile ──tools/import_shapes.py──▶ staging.site_* ──shape_key 去重──▶ features
features ──GET /api/features──▶ OpenLayers 2D / 属性面板 / 编辑表单
features ──GET /api/tiles/{z}/{x}/{y}.pbf──▶ MVT（外部调用方）
features ──GET /api/features──▶ Three.js 2D/3D 切换
```

## 4. 快速开始

### 4.1 SQLite（默认，无需数据库）

```powershell
cd project5_map_platform

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

.\.venv\Scripts\python.exe -m uvicorn app:app --port 8000
```

首次启动会创建 `data/map.db`，并把 `data/site.geojson` 的 9 个要素写入空表。浏览器打开 <http://127.0.0.1:8000>，状态栏应显示：

```
要素 9 个 · 后端 sqlite · 底图 本地网格（离线）
```

编辑令牌默认 `demo-token`，已预填在页面右上角。

### 4.2 PostGIS

```powershell
$psql = "C:\Program Files\PostgreSQL\17\bin\psql.exe"
& $psql -U postgres -c "CREATE DATABASE mapdemo;"
& $psql -U postgres -d mapdemo -c "CREATE EXTENSION IF NOT EXISTS postgis;"

$env:DATABASE_URL = "postgresql://postgres:你的密码@127.0.0.1:5432/mapdemo"
.\.venv\Scripts\python.exe -m uvicorn app:app --port 8000
```

密码含 `@ # / : ? %` 等字符时先 URL 编码：

```powershell
$rawPwd = 'p@ss#word:1'
$encPwd = [uri]::EscapeDataString($rawPwd)
$env:DATABASE_URL = "postgresql://postgres:$encPwd@127.0.0.1:5432/mapdemo"
```

启动时 `pg_init_db()` 会建表、建 GiST 索引、缺 `version` 列时补齐，并在空表时导入种子数据。连接失败时进程直接退出，不会回退到 SQLite，避免写错库；错误信息会打出 DSN 的 host/port/dbname/user（隐藏密码）和底层异常。

状态栏应显示 `后端 postgis`。`/api/tiles` 响应头为 `X-Tile-Source: postgis-st_asmvt`。

### 4.3 没有 FastAPI 时的后备启动

`server_offline.py` 用标准库 `http.server` 提供只读页面和部分接口，适合只装了 Python 的机器：

```powershell
.\.venv\Scripts\python.exe server_offline.py 8000
```

它支持 `GET /`、`/api/health`、`/api/stats`、`/api/features`、`/api/tiles`（返回 GeoJSON）和 `POST /api/features`；不支持 `PUT` 编辑、`/api/metrics`、`/api/tilejson.json`，`/api/health` 的 `backend` 为 `sqlite-offline`。

### 4.4 测试与验证

```powershell
pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe tools\verify_pg.py
.\.venv\Scripts\python.exe tools\verify_offline.py
.\.venv\Scripts\python.exe tools\verify_edit.py
```

`verify_pg.py` 直接请求 PostGIS 的接口和瓦片；`verify_offline.py`、`verify_edit.py` 用 Chrome 无头模式 + CDP 检查真实页面：离线环境只能请求本机、要素数、编辑后 `version` 递增、外部请求为 0。它们依赖 `requests`、`websocket-client`，这两个包只在 `requirements-dev.txt` 里，只装运行依赖时脚本会报 `ModuleNotFoundError`。

CI 在 GitHub Actions 上运行（`.github/workflows/ci.yml`）：`push` 到 `main` 和 `pull_request` 时，用 `ubuntu-latest` + Python 3.10 安装 `requirements.txt` 与 `requirements-test.txt`，执行 `pytest -q`。CI 只覆盖 SQLite 路径，不安装 PostGIS，也不跑需要 Chrome 的 `verify_*.py`。

实际结果：run #1（`push` 触发，commit `9b7b5c0`）为 success，见 <https://github.com/evenxu777/hdmap-platform/actions/runs/42600930948>；`pull_request` 触发条件已配置，但还没有用 PR 实测过。

## 5. 数据准备

### 5.1 示例数据

`data/site.geojson` 是自造的模拟矿区数据，共 9 个要素，范围 `120.600, 31.300 ~ 120.612, 31.3065`：

| 几何类型 | 数量 | 名称 |
| --- | --- | --- |
| Point | 4 | 装载点-A、卸载点-B、加油点-1、破碎站 |
| LineString | 2 | 主运输道路、装载区支路 |
| Polygon | 3 | 采区围栏、排土场围栏、料堆-1 |

`kind` 分布：`road` 2、`geofence` 2，`load`/`unload`/`fuel`/`crusher`/`stockpile` 各 1。表为空时自动导入；要重置示例数据，停止服务后删除 `data/map.db`（SQLite）或清空 `features` 表（PostGIS）。

### 5.2 Shapefile 入库（PostGIS）

`tools/import_shapes.py` 的链路：

```
data/site.geojson
  → 按几何类型拆成 point/line/polygon 三套 Shapefile（临时目录）
  → ogr2ogr -f PGDUMP 生成 SQL
  → psycopg2 执行到 staging.site_*
  → SQL 合并进 features，按 props->>'shape_key' 跳过已存在记录
```

运行：

```powershell
$env:DATABASE_URL = "postgresql://postgres:真实密码@127.0.0.1:5432/mapdemo"
.\.venv\Scripts\python.exe tools\import_shapes.py
```

脚本没有命令行参数，只读环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | 无 | 必须以 `postgres` 开头，否则直接退出 |
| `PGBIN` | `C:\Program Files\PostgreSQL\17\bin` | 找 `ogr2ogr.exe`、`psql.exe` |
| `POSTGIS_BUNDLE` | 本机安装路径 | 第二个 `ogr2ogr` 候选目录 |

DBF 字段名最多 10 个字符，脚本只保留短字段，完整属性进 `props_json`。合并时要求几何非空、SRID 为 4326，不符合的记录会单独计数。重复运行不会新增数据：示例数据第一次合并后总数为 18（种子 9 + Shapefile 9），第二次新增 0。

## 6. API 一览

### 6.1 通用约定

- 几何统一使用 WGS84（EPSG:4326）经纬度。
- 写接口需要请求头 `X-Edit-Token`，默认值 `demo-token`。
- 更新接口必须带 `version`，服务端执行 `UPDATE ... WHERE id=? AND version=?`，成功后 `version+1`。
- 错误响应统一为：

```json
{"error": {"code": "VERSION_CONFLICT", "message": "...", "request_id": "...", "details": {}}}
```

- 每个响应都带 `X-Request-ID`；请求头传入同名值时沿用。

### 6.2 接口

| 方法 | 路径 | 参数 | 返回 |
| --- | --- | --- | --- |
| GET | `/api/health` | 无 | `{"ok": true, "backend": "sqlite" \| "postgis"}` |
| GET | `/api/stats` | 无 | `{"total": 9, "by_kind": {...}}` |
| GET | `/api/features` | `bbox=minx,miny,maxx,maxy`、`kind` 均可选 | GeoJSON FeatureCollection |
| GET | `/api/features/{id}` | 路径参数 | 单个 Feature，含 `id`、`version`、`properties` |
| POST | `/api/features` | body：`kind`、`name`、`properties`、`geometry`；header：`X-Edit-Token` | `{"ok": true, "id": 10, "version": 1}` |
| PUT | `/api/features/{id}` | body 额外必填 `version`；header：`X-Edit-Token` | `{"ok": true, "id": 10, "version": 2}`；版本不匹配返回 409 |
| GET | `/api/tiles/{z}/{x}/{y}.pbf` | `z` 范围 0~22 | MVT；PostGIS 响应头 `X-Tile-Source: postgis-st_asmvt`，Python 路径带 `X-Feature-Count` |
| GET | `/api/tilejson.json` | 无 | TileJSON 3.0，`scheme=xyz`，bounds 固定为示例范围附近 |
| GET | `/api/metrics` | 无 | Prometheus 文本格式，进程内累计，重启清零 |
| GET | `/`、`/static/*` | 无 | 前端页面与静态资源 |

`kind` 白名单：`road`、`load`、`unload`、`fuel`、`crusher`、`stockpile`、`geofence`、`parking`、`dump`。几何白名单：`Point`、`LineString`、`Polygon` 及其 `Multi*` 版本；页面上的新增和编辑入口只开放点位。

### 6.3 调用示例

```powershell
curl.exe -s http://127.0.0.1:8000/api/health
curl.exe -s "http://127.0.0.1:8000/api/features?kind=load"
curl.exe -s -o tile.pbf -D - "http://127.0.0.1:8000/api/tiles/16/54723/26763.pbf"
```

新增点位：

```powershell
curl.exe -s -X POST http://127.0.0.1:8000/api/features -H "Content-Type: application/json" -H "X-Edit-Token: demo-token" -d '{\"kind\":\"load\",\"name\":\"装载点-C\",\"properties\":{},\"geometry\":{\"type\":\"Point\",\"coordinates\":[120.608,31.303]}}'
```

更新时把返回的 `version` 带上；如果页面同时编辑同一条记录，后提交的一方会收到 409 `VERSION_CONFLICT`。

## 7. 配置项

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `DATABASE_URL` | 空 | 空值走 SQLite；以 `postgres` 开头启用 PostGIS，不支持的 scheme 会导致启动失败 |
| `SQLITE_PATH` | `data/map.db` | SQLite 文件位置 |
| `EDIT_TOKEN` | `demo-token` | `POST`/`PUT` 的编辑令牌 |
| `PGBIN` | `C:\Program Files\PostgreSQL\17\bin` | Shapefile 入库脚本的工具目录 |
| `POSTGIS_BUNDLE` | 本机路径 | Shapefile 入库脚本的备用工具目录 |

没有 `.env` 加载逻辑，配置一律从进程环境变量读取。

## 8. 目录结构

```
project5_map_platform/
├─ app.py                  FastAPI 路由、鉴权、错误体、指标、请求 ID
├─ db.py                   SQLite/PostGIS 建表、查询、更新、种子导入、连接诊断
├─ tiles.py                经纬度/瓦片换算、Python MVT 编码
├─ server_offline.py       标准库后备服务（只读页面 + 部分接口）
├─ static/
│  ├─ index.html           OpenLayers 2D + Three.js 简易 3D + 编辑表单
│  └─ vendor/              ol.js、ol.css、three.min.js 本地副本
├─ data/
│  ├─ site.geojson         示例数据（9 个要素）
│  └─ map.db               SQLite 数据文件（首次启动生成）
├─ tools/
│  ├─ import_shapes.py     GeoJSON → Shapefile → PGDUMP → staging → features
│  ├─ verify_pg.py         PostGIS 接口与瓦片检查
│  ├─ verify_offline.py    离线页面渲染检查
│  └─ verify_edit.py       新增/编辑/版本冲突检查
├─ tests/test_api.py       SQLite 路径的接口测试
├─ sql/init_postgis.sql    建库与扩展初始化参考
├─ docs/                   口述文案与演示脚本
├─ .github/workflows/ci.yml  GitHub Actions：Python 3.10 + pytest
├─ requirements.txt        运行依赖
├─ requirements-test.txt   CI/测试依赖（pytest、httpx）
├─ requirements-dev.txt    测试与验证依赖（含 verify_* 所需包）
├─ Dockerfile
└─ docker-compose.yml
```

## 9. 已知限制与未实现项

- **前端不消费 MVT**：`static/index.html` 只用 `/api/features` 的 GeoJSON，浏览器里不会出现 `.pbf` 请求。瓦片接口是给外部调用方用的；如果页面空白，先查 `/api/features`，不要先怀疑瓦片。
- **PostGIS 没有可用兜底**：`/api/tiles` 在 `ST_AsMVT` 失败后会落到按 SQLite 行结构写的 Python 分支，而 PostGIS 行里没有 `minx/maxx` 等字段，先触发 `KeyError`；即使走到 GeoJSON 降级，`geometry` 已是 dict，`to_geojson()` 里再次 `json.loads` 会报错。SQLite 路径的 Python 编码与 GeoJSON 降级正常。
- **`HEAD /api/tiles/{z}/{x}/{y}.pbf` 返回 404**：路由只注册了 `GET`，用 `curl -I` 探测会误判为接口不存在。
- **验证脚本依赖开发包**：`tools/verify_offline.py` 等需要 `requests`、`websocket-client`，只执行 `pip install -r requirements.txt` 后运行会缺包；开发依赖在 `requirements-dev.txt`。
- **鉴权只有一个静态令牌**：没有用户、角色、审计；令牌默认值写在页面里；CORS 为 `*`，不能直接暴露到公网。
- **并发只有乐观锁**：`version` 不匹配返回 409，没有自动重试、合并或冲突 UI。
- **Python 兜底路径性能有限**：先全表取出再按 `bbox` 过滤，数据量到万级以上会明显变慢；SQLite 的 `minx/miny/maxx/maxy` 是普通联合索引，不是空间索引。
- **瓦片按需生成、无缓存**：没有预切片、ETag、CDN；PostGIS 路径依赖 GiST 索引，未做压测，不提供吞吐数据。
- **导入限制**：只接受 SRID 4326 且非空几何的 Shapefile；DBF 字段名按 10 字符截断；不支持多图层、编码非 UTF-8 的 DBF、批量事务调优。
- **3D 是示意级**：多边形按 `height_m` 或默认高度拉伸，点位是球体，没有地形、点云、倾斜摄影或 3D Tiles。
- **CI 覆盖范围有限**：GitHub Actions 已接入（run #1 `push` 触发为 success，`pull_request` 触发尚未实测），但只跑 `tests/test_api.py` 的 SQLite 路径；PostGIS 和 `verify_*.py` 的浏览器检查仍是本地手动执行。`Dockerfile`、`docker-compose.yml` 存在，但本机没有 Docker，未实际构建运行。
- **PostGIS 只在单机验证过**：PostgreSQL 17 + PostGIS 3.6，库名 `mapdemo`；没有多实例、主从或连接池配置。

## 10. 许可证与数据来源

- 仓库目前没有附带 `LICENSE` 文件，默认按内部工具使用；对外分发前需要补充许可证。
- `data/site.geojson` 及派生 Shapefile 是自造的模拟数据，不含真实矿区数据、个人位置或第三方受限数据。
- 第三方依赖遵循各自许可证：FastAPI/Starlette（MIT）、OpenLayers（BSD-2-Clause）、Three.js（MIT）、mapbox-vector-tile（MIT）、psycopg2（LGPL with exceptions）。
- 底图默认是本地绘制的网格，不请求外部瓦片；点击“在线底图”会切到 OpenStreetMap，使用需遵守 OSM 的使用政策。
