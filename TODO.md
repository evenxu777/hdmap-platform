# 项目⑤ 下一步 TODO

## 必做（做完才算"可展示"）
> 2026-11-05 复核：第 1–7 项与 pytest 已实测通过；录屏仍缺。

- [x] FastAPI 后端 + SQLite 建库 + 种子数据
- [x] GeoJSON 要素查询接口
- [x] MVT 瓦片接口（含 GeoJSON 降级）
- [x] 新增要素接口 + 令牌校验
- [x] OpenLayers 2D 展示 + 点选 + 新增点位
- [x] Three.js 3D 拉伸
- [x] 前端库本地化（断网可演示，2026-11-05 验证 `static/vendor/` 三个文件存在）
- [ ] 3 分钟录屏 + 截图进 README（4 张截图已在 `docs/screenshots/`；录屏文件仍缺失）
- [x] pytest 至少 5 个接口测试（实际 7 passed，2026-11-05）

## 加分（面试讲规划用）
- [ ] PostGIS `ST_AsMVT` + `ST_TileEnvelope` 瓦片路径（`db.pg_tile_mvt` 已写入；本机无 Docker，未实测）
- [ ] 要素编辑版本号 / 乐观锁
- [ ] JWT + 角色权限
- [ ] 编辑历史与回滚
- [ ] 瓦片缓存（ETag / Cache-Control）
- [ ] 点云接入（PDAL → 3D Tiles 或简单点渲染）
- [ ] CI：GitHub Actions（lint + pytest）
