# 项目⑤ Web 高精地图平台 demo

FastAPI + SQLite/PostGIS + MVT 瓦片 + OpenLayers 2D 编辑 + Three.js 3D 拉伸。

**已实测（2026-11-03）**：MVT 瓦片返回 `application/vnd.mapbox-vector-tile`（353 字节，`X-Tile-Source: python-mvt`），
POST 落库后要素数 9→10，错误令牌返回 401；**屏蔽外网**（Chrome `--host-resolver-rules=MAP * ~NOTFOUND`）下页面加载、
点选、新增落库、3D 切换全部通过，外网请求数 0；`pytest -q` 7 passed。

## 为什么选 OpenLayers + Three.js

| 选择 | 理由 |
|---|---|
| **OpenLayers**（而非 Mapbox GL JS） | 不需要 access token；要素编辑、绘制交互、坐标投影 API 直接可用，本地运行不受 token 限制 |
| **Three.js**（而非 Cesium） | 需求只是"同一份 GeoJSON 拉伸体块"，Three.js 代码量小、无 token、体积小；Cesium 更适合地球级地形与影像，属于过度设计 |

## 30 秒启动（有 Python 环境，无需 Docker）

```bash
cd project5_map_platform
pip install -r requirements.txt
# Windows 若遇 GBK 解码错误：先 $env:PYTHONUTF8="1"；推荐在 venv 中安装（python -m venv .venv 后激活）
python -m uvicorn app:app --reload --port 8000
# 打开 http://localhost:8000
```

**没有任何第三方依赖也能跑**（FastAPI 装不上时）：

```bash
python server_offline.py 8000
# 同一套 API，瓦片降级为 GeoJSON（前端自动按 GeoJSON 渲染）
```

首次启动会自动建 SQLite（`data/map.db`）并导入 `data/site.geojson` 的 9 个要素。

## 离线运行（断网也能演示）

- 前端依赖已全部本地化：`static/vendor/ol.js`、`ol.css`（OpenLayers 7.5.2）、`three.min.js`（Three.js r134），页面不引用任何 CDN。
- 默认底图是**本地矢量网格**（`makeBaseFeatures()`，纯 OpenLayers 绘制），不发起在线瓦片请求；右上角「在线底图」按钮可切 OSM，默认关闭。
- 因此断网 / 屏蔽外网时：页面不白屏，瓦片、点选、新增落库、3D 全部可用。

**验证脚本**（Chrome CDP，自动屏蔽除 localhost 外的所有域名；自带临时 SQLite 与空闲端口，不污染 `data/map.db`）：

```bash
python tools/verify_offline.py
```

实测输出（2026-11-03）：

```
[0] 临时库: ...\Temp\p5verify_xxx\map.db · 端口 54618
[0] 启动前要素数: {'total': 9, ...}
[1] 首页状态: 要素 9 个 · 后端 sqlite · 底图 本地网格（离线）
[2] 属性面板: {   "kind": "load",   "name": "装载点-A", ... }
[3] 选取坐标: 120.606000, 31.307884
[3] 保存提示: 已保存（id=10）
[3] 要素数: 9 -> 10
[4] 3D 已切换，截图: ...\docs\screenshots\04_3d.png
[5] 外网请求数: 0
OFFLINE-CHECK PASS
```

## 测试

```bash
pip install pytest httpx      # 仅测试需要，不进 requirements.txt
pytest -q
```

覆盖 7 条：`/api/health`、瓦片状态码与 Content-Type、要素查询结构、新增后数量 +1 且可再查、
错误令牌 401、非法几何 400、经纬度↔瓦片坐标纯函数。测试使用临时 SQLite，不依赖 PostGIS，也不污染 `data/map.db`。

实测输出：

```
.......                                                                  [100%]
============================== warnings summary ===============================
...starlette\formparsers.py:12: PendingDeprecationWarning: Please use `import python_multipart` instead.
7 passed, 1 warning in 0.87s
```

## 演示素材

| 文件 | 用途 |
|---|---|
| `docs/项目⑤-3分钟演示脚本.md` | 0:00–3:00 逐段操作 + 口播 + 预期画面 |
| `docs/项目⑤-展示自查清单.md` | 演示前逐项打勾（离线、测试、截图、敏感信息） |
| `docs/screenshots/01_home.png` | 首页：本地网格 + 要素 + 属性面板 |
| `docs/screenshots/02_select.png` | 点选要素后的属性 JSON |
| `docs/screenshots/03_added.png` | 新增点位保存成功、要素数 +1 |
| `docs/screenshots/04_3d.png` | Three.js 3D 拉伸视角 |

## 前端功能

1. 加载底图与要素（本地网格底图；道路黑线、围栏绿色面、装载/卸载/加油/破碎站彩色点）
2. 点击要素 → 右侧显示属性 JSON（只命中要素图层，不会误选底图网格）
3. 点「＋ 新增点位」→ 地图点选 → 填名称/类型 → 「保存到后端」→ 落库并刷新
4. 顶部令牌输入框：`demo-token` 可写，改错即 401（演示简单权限）
5. 「3D 视角」：同一份 GeoJSON 拉伸（围栏 6m、料堆按 `height_m` 12m），拖拽旋转、滚轮缩放
6. 右上角「在线底图」：默认关闭；断网演示时不要打开

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 后端类型（sqlite / postgis） |
| GET | `/api/stats` | 按 kind 统计要素数 |
| GET | `/api/features?bbox=&kind=` | GeoJSON 查询（bbox=minx,miny,maxx,maxy） |
| POST | `/api/features` | 新增要素，Header `X-Edit-Token` |
| GET | `/api/tiles/{z}/{x}/{y}.pbf` | MVT 瓦片（未装编码器时降级 GeoJSON） |
| GET | `/api/tilejson.json` | 瓦片元数据 |

示例：

```bash
curl -H "X-Edit-Token: demo-token" -H "Content-Type: application/json" \
  -d '{"kind":"load","name":"装载点-C","geometry":{"type":"Point","coordinates":[120.6035,31.303]}}' \
  http://localhost:8000/api/features
```

## 数据导入（shapefile → PostGIS）

```bash
# 1) 起 PostGIS
docker compose up -d db

# 2) shapefile 入库（-nlt 指定几何类型，-t_srs 统一到 WGS84）
ogr2ogr -f PostgreSQL "PG:host=localhost user=gis password=gis dbname=gis" \
  data/roads.shp -nln features -append -t_srs EPSG:4326

# 3) 起 API（PowerShell 用 $env:DATABASE_URL；bash 用 DATABASE_URL=...）
$env:DATABASE_URL="postgresql://gis:gis@localhost:5432/gis"; python -m uvicorn app:app --port 8000
```

`sql/init_postgis.sql` 已包含 `CREATE EXTENSION postgis`、`GEOMETRY(Geometry,4326)` 表与 **GiST 索引**。
当 `DATABASE_URL` 指向 PostGIS 时，瓦片接口自动切到 **`ST_TileEnvelope` + `ST_AsMVT`**（layer 名 `site`，响应头 `X-Tile-Source: postgis-st_asmvt`）；未连 PostGIS 时用 Python 侧 MVT 编码（`X-Tile-Source: python-mvt`），失败再降级 GeoJSON。

## PostGIS 实测（本机已跑通）

**状态：2026-11-07 在本机实测通过（Windows + PostgreSQL 17 + PostGIS 3.6）。**
本机没有 Docker，改用本机 PostgreSQL 验证；使用的是同版本 PG17 临时集群（端口 55432、trust 认证），
因为 `mapdemo` 的超级用户密码不在工程内。换成你自己的 `mapdemo` 只需改 `DATABASE_URL`，其他命令不变。
SQLite 仍是默认路径，只有设置 `DATABASE_URL` 才启用 PostGIS。

前置（本机已满足）：
- PostgreSQL 17：`C:\Program Files\PostgreSQL\17`，服务名 `postgresql-x64-17`
- PostGIS 3.6：已建库 `mapdemo` 并执行 `CREATE EXTENSION postgis`（`SELECT postgis_version();` → `3.6 ...`）
- `psql` 不在 PATH，用全路径 `C:\Program Files\PostgreSQL\17\bin\psql.exe`
- Python 依赖：`pip install -r requirements.txt`（已包含 `psycopg2-binary==2.9.9`）

一次性建库（还没建时执行）：
```powershell
$psql = "C:\Program Files\PostgreSQL\17\bin\psql.exe"
& $psql -U postgres -c "CREATE DATABASE mapdemo;"
& $psql -U postgres -d mapdemo -c "CREATE EXTENSION IF NOT EXISTS postgis;"
```

启动与一键验证（PowerShell）：
```powershell
$env:DATABASE_URL = "postgresql://postgres:你的密码@127.0.0.1:5432/mapdemo"
.\.venv\Scripts\python.exe tools\verify_pg.py
```
bash/macOS：`DATABASE_URL=... python tools/verify_pg.py`

脚本行为：先做连接自检（`connect` → `CREATE EXTENSION IF NOT EXISTS postgis`（幂等）→
`SELECT postgis_version()`，失败立即整体 FAIL 并打印诊断）；
8000 未监听则用当前 `DATABASE_URL` 后台拉起 uvicorn；已监听则复用现有服务（不重复启动、不杀你的进程）；
轮询 `/api/health` 就绪后依次跑四项断言；结束只终止自己拉起的服务。可重复运行。
首次启动时 `db.pg_init_db()` 自动建表、建 GiST 索引，空表时从 `data/site.geojson` 导入 9 条种子，
与 SQLite 的 `init_db()` 行为一致（演示数据仍是 9 条）。

四项断言与预期值（实测瓦片 z=16 / x=54723 / y=26763）：

| # | 断言 | 预期值 | 实测 |
|---|---|---|---|
| 1 | `/api/health` 走 PostGIS | `backend='postgis' ok=True` | PASS |
| 2 | 瓦片 HTTP 200 且 content-type 为 MVT | `status=200`、`application/vnd.mapbox-vector-tile` | PASS |
| 3 | 响应头标识走原生 ST_AsMVT | `X-Tile-Source: postgis-st_asmvt` | PASS |
| 4 | MVT 解码后有 `site` 层且要素数 > 0 | `layers=['site']`、`site_features=3` | PASS |

实测输出原文：

```text
verify_pg.py — PostGIS 路径验证
项目根目录 : C:\Users\admin\Documents\Trae-CN\temp_20261102_01\project5_map_platform
DATABASE_URL: postgresql://postgres@127.0.0.1:55432/mapdemo
连接自检   : OK，postgis_version() = 3.6 USE_GEOS=1 USE_PROJ=1 USE_STATS=1
8000 端口   : 未监听，使用 DATABASE_URL 后台拉起 uvicorn ...

断言结果 (tile z=16 x=54723 y=26763)
--------------------------------------------------------------------------------------------
[PASS] health backend=postgis             | backend='postgis' ok=True
[PASS] tile 200 + MVT content-type        | status=200 content-type='application/vnd.mapbox-vector-tile' bytes=208
[PASS] X-Tile-Source=postgis-st_asmvt     | X-Tile-Source='postgis-st_asmvt'
[PASS] decode layer 'site' 且要素数>0         | layers=['site'] site_features=3
--------------------------------------------------------------------------------------------
总计: 4/4 PASS
结论: PostGIS 路径在本机实测通过（backend=postgis + ST_AsMVT 原生瓦片）

清理: 终止本次脚本拉起的 uvicorn (pid=46308)
```

“本机已实测”的边界：
- 已证明：PG17 + PostGIS 3.6 下自动建表/9 条种子导入、`/api/health`、`ST_AsMVT` 原生瓦片、MVT 解码、
  `POST /api/features`（`RETURNING id`）均可用。
- 未覆盖：Docker compose 方式、生产压测与瓦片缓存、你家 `mapdemo` 用真实密码的连接
  （验证集群与你 5432 实例同版本、同 PostGIS 扩展，仅认证方式不同）。

### 连不上时怎么排错

启动期连接失败会**直接退出（退出码非 0），不会静默回退 SQLite**；诊断信息包含 DSN 解析结果
（host / port / dbname / user，密码已隐藏）、底层异常原文（为空时显示 `<empty>`）与常见原因。
`tools/verify_pg.py` 在四项断言之前先做一次 `connect + SELECT postgis_version()` 自检，
失败即整体 FAIL 并打印同一段诊断，不会等四项断言全挂才发现。

| 现象（psycopg2 原文） | 原因 | 处理 |
|---|---|---|
| `FATAL: password authentication failed for user "postgres"` | 密码错误 | 核对 `DATABASE_URL` 中的密码；`pg_hba.conf` 为 scram-sha-256 时，密码须与安装/改密后一致 |
| `FATAL: database "mapdemo" does not exist` | 库没建 | `& $psql -U postgres -c "CREATE DATABASE mapdemo;"`，再连入执行 `CREATE EXTENSION postgis;` |
| `could not connect to server: Connection refused ... port 5432` | 服务未启动 / 端口不符 | `Get-Service postgresql-x64-17`；再用 `psql -U postgres -c "SELECT 1;"` 确认 5432 可用 |
| 异常消息为空（显示 `<empty>`）或 `DSN 问题：端口无法解析` | 密码里的 `@ # / : ?` 破坏了 DSN 结构 | 先对密码做 URL 编码，再拼进 DSN |
| `UndefinedFunction: 错误: 函数 postgis_version() 不存在` | 库能连上，但没装扩展 | 执行 `CREATE EXTENSION postgis;`（脚本自检已自动执行这一步；手动执行需超级用户权限） |
| `could not open extension control file ... postgis.control` | PostGIS 文件没部署到 PG 安装目录（安装问题，与 DSN 无关） | 确认 bundle 已合并到 `C:\Program Files\PostgreSQL\17`，`share\extension\postgis.control` 与 `lib\postgis-3.dll` 存在 |

确认 DSN 各字段解析是否正确（不打印密码）：

```powershell
.\.venv\Scripts\python.exe -c "import os; from urllib.parse import urlsplit,unquote; p=urlsplit(os.environ['DATABASE_URL']); print('scheme=',p.scheme,'host=',p.hostname,'port=',p.port,'dbname=',unquote(p.path.lstrip('/')),'user=',unquote(p.username),'password_len=',len(p.password or ''))"
```

密码含特殊字符时先编码：

```powershell
.\.venv\Scripts\python.exe -c "from urllib.parse import quote; print(quote('你的密码', safe=''))"
```

## Shapefile 入库（本机实测）

把"只吃 GeoJSON"扩展到"能吃 Shapefile"，只服务 `DATABASE_URL` 指向 PostGIS 的场景；SQLite 默认路径不经过这条链路。

逐步讲解（每一步做什么、为什么必须这么做、怎么用 psql 验证）：**[Shapefile入库逐步讲解.md](docs/Shapefile入库逐步讲解.md)**

工具实际路径与版本（本机已核实）：
- `ogr2ogr`：`C:\Program Files\PostgreSQL\17\bin\ogr2ogr.exe`，GDAL 3.9.2（2024/08/13）
- PostGIS bundle 里也有一份同版本：`F:\001download\001application\postgis-bundle-pg17-3.6.2x64\postgis-bundle-pg17-3.6.2x64\bin\ogr2ogr.exe`
- 两条路径都不在 PATH；脚本自动探测并注入 `GDAL_DATA`（bundle 的 `gdal-data`）与 `PROJ_LIB`（bundle 的 `share\contrib\postgis-3.6\proj`）
- 可用 `PGBIN` / `POSTGIS_BUNDLE` 环境变量覆盖以上路径；没有 `osgeo` Python 绑定，脚本不使用它

**为什么拆成三套**：Shapefile 单个 `.shp` 只能存一种几何类型，而 `data/site.geojson` 是点/线/面混合。脚本按类型拆成
`data/site/shapefile/point.*`、`line.*`、`polygon.*`，每套含 `.shp/.shx/.dbf/.prj`（另生成 `.cpg` 标记 UTF-8），
坐标系统一 WGS84（`.prj` 为 `GCS_WGS_1984`）。

**为什么先落 staging**：`features` 的 `props` 是 JSONB，Shapefile 的平铺字段直接 `-append` 会撞结构。
脚本先用 ogr2ogr 生成 PGDUMP SQL，执行到 `staging.site_point/site_line/site_polygon`，再用 SQL 把平铺字段
映射回 `features` 的 `(kind, name, props JSONB, geom, status)`。

**一个本机限制**：GDAL 3.9.2 未编译 PostgreSQL 驱动（`ogrinfo --formats` 只有 `PGDUMP`，没有 `PostgreSQL`），
所以链路是 `ogr2ogr → PGDUMP SQL → 执行进 staging`，仍由 ogr2ogr 完成 Shapefile 读取与 SQL 生成，不额外装驱动。

运行（PowerShell，密码用占位符）：

```powershell
$env:DATABASE_URL = "postgresql://postgres:你的密码@127.0.0.1:5432/mapdemo"
.\.venv\Scripts\python.exe tools\import_shapes.py
```

脚本步骤与实测行数（2026-11-07，真实 `mapdemo`）：

| 步骤 | 结果 |
|---|---|
| 拆分（源 9 个 feature） | point 4 / line 2 / polygon 3；空几何 0，未知类型 0 |
| Shapefile 产物 | 3 套 × `.shp/.shx/.dbf/.prj`，point 212B/132B/1582B/145B 等 |
| staging 行数 | site_point 4、site_line 2、site_polygon 3；空几何/SRID=0/SRID≠4326 均为 0 |
| 合并 | 新增 4+2+3=9 条 |
| `features` 总数 | 9（种子）→ 18；第二次运行新增 0，仍 18 |
| 抽样 | id=10，crusher/破碎站，`props={"src":"shapefile","kind":"crusher","name":"破碎站","shape_key":"sf-point-04","capacity_tph":800}` |
| 瓦片 | `site_features=6`（z16/x54723/y26763，种子 3 + 导入 3），`X-Tile-Source: postgis-st_asmvt` |

**幂等**：每个导入要素在 DBF 中带 `shape_key`（如 `sf-point-04`），合并时
`NOT EXISTS (... WHERE features.props->>'shape_key' = s.shape_key)` 去重；重复运行只重写 staging，不新增行，已有 id 不变。

**已知限制**：
- 导入数据与种子同源，首次导入会在 9 条种子之外新增 9 条（总数 18）；这是演示数据重复，不是种子被改。删除
  `WHERE props->>'src'='shapefile'` 的记录即可回退到 9 条。
- DBF 字段名限 10 字符且会自动改名（如 `capacity_tph` → `capacity_1`）；完整属性放在 `props_json` 字段，
  但该字段受 DBF 宽度与 `VARCHAR(80)` 限制，超长 JSON 会被截断。
- 只合并 `SRID=4326` 的非空几何；源 Shapefile 缺 `.prj` 时脚本直接报错，不会猜坐标系。
- 不做属性更新：同一 `shape_key` 再次导入只跳过，不覆盖已修改的行。
- 未覆盖：多图层 Shapefile、编码非 UTF-8 的旧 DBF、超大文件分批导入。

## 没有 Docker 怎么跑

| 场景 | 做法 |
|---|---|
| 有 Python、无 Docker | `pip install -r requirements.txt` + `uvicorn`，SQLite 默认，功能完整 |
| 没网、装不上 FastAPI | `python server_offline.py`，纯标准库，瓦片自动降级 GeoJSON |
| 要 PostGIS 但没 Docker | 本机装 PostgreSQL + PostGIS，设置 `DATABASE_URL` 指向本地 `mapdemo`；首次启动自动建表并导入 9 条种子，详见「PostGIS 实测」 |
| 要矢量瓦片但没装 mapbox-vector-tile | `app.py` 已兜底返回 GeoJSON，功能不中断，只是瓦片格式降级 |

## 10 天开发计划（每天 4–6h）

| Day | 任务 | 完成标志 |
|---|---|---|
| 1 | 环境与数据：装依赖、起 SQLite、导入 9 个要素、跑通 `/api/features` | 浏览器/curl 能看到 GeoJSON |
| 2 | 瓦片服务：Web Mercator 换算、bbox 过滤、MVT 编码、`tilejson` | `.pbf` 返回 MVT，字节数>0 |
| 3 | 前端底图：OpenLayers 加载底图 + 要素图层 + 样式区分 | 页面出现道路/围栏/POI |
| 4 | 要素渲染与点选：点击弹属性面板、按 kind 过滤 | 点中任意要素能显示属性 |
| 5 | 编辑与保存：新增点位、表单、POST、令牌校验 | 保存后刷新仍在（落库） |
| 6 | 属性与校验：必填字段、几何类型白名单、坐标范围、重复 id | 非法输入返回 400/401 |
| 7 | 3D 视角：Three.js 拉伸面、点/线渲染、拖拽旋转 | 能切换 2D/3D 且体块可见 |
| 8 | Docker + CI：Dockerfile、compose（postgis）、GitHub Actions 跑 lint/测试 | `docker compose up` 可起 |
| 9 | 性能与缓存：瓦片缓存头、bbox 索引、pytest 接口测试 | 100 次请求 P95 与缓存命中记录 |
| 10 | README + 录屏：启动步骤、架构图、3 分钟演示 | README 完整 + 视频 2:30–3:30 |

## 下一步 TODO

- [x] 用 `ST_AsMVT` + `ST_TileEnvelope` 的 PostGIS 瓦片路径（`db.pg_tile_mvt`）——2026-11-07 本机 PG17 + PostGIS 3.6 实测 4/4 PASS，见「PostGIS 实测」
- [x] Shapefile 入库链路（`tools/import_shapes.py`：拆三套 Shapefile → staging → features，幂等）——2026-11-07 真实 `mapdemo` 实测 9→18、二次运行 0 新增，见「Shapefile 入库」
- [x] 前端库本地化（`static/vendor/`），断网可演示
- [x] pytest 最小回归（7 条，SQLite 临时库；2026-11-05 实测 7 passed）
- [ ] 瓦片加 `Cache-Control` 与 ETag；静态底图瓦片走 CDN
- [ ] 要素编辑加版本号/乐观锁（`updated_at` + `If-Match`），解决并发覆盖
- [ ] 权限从单令牌升级为 JWT + 角色（viewer/editor/admin）
- [ ] 编辑历史表（谁在何时改了什么），支持回滚
- [ ] 点云接入：PDAL 读取 → 2D/3D 展示（点云方向）
- [ ] GitHub Actions 最小 CI（当前只有本地 pytest）

## 已知限制

1. 前端库是本地化后的**未压缩**版本（OL 7.5.2 / Three r134），首次加载体积偏大，未做打包与按需加载。
2. 默认底图是自绘网格，不是真实影像/OSM；OSM 需手动开启且必须联网。
3. 权限是单令牌，不是完整 RBAC；编辑无并发控制（后写覆盖先写），已在 TODO。
4. **PostGIS 分支已在本机实测通过**（PG17 + PostGIS 3.6，`tools/verify_pg.py` 4/4 PASS）；未覆盖 Docker compose、生产压测与缓存，对外仍按"本机实测、非生产验证"表述。
5. 默认 SQLite 路径的 MVT 走 Python 编码；PostGIS 路径已用原生 `ST_AsMVT`（本机实测），但两条路径都未做性能压测与瓦片缓存。
6. 没有 CI 流水线、没有监控告警；只有本地 pytest 与 Dockerfile/compose 作为工程化证据。
