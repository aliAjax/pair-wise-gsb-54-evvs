# 跨海光缆故障与抢修协调

纯Python标准库实现的跨海光缆故障与抢修协调原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、海况、船机许可、备缆窗口、接续质量和船舶资源占用计划。
- `src/repository.py`：SQLite建表、事务和查询，船舶资源核对与扣减在记录变更的同一事务内完成。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、资源占用和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8330
```

默认端口为`8330`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `GET /api/vessels`：船舶列表，含备缆容量、预留、消耗与剩余。
- `POST /api/vessels`：登记船舶，请求体为`{"name":"CS-1","spare_capacity_km":30}`。
- `GET /api/vessels/{name}`：船舶详情与在修占用（哪项抢修、时段、预留、消耗）。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 资源占用

- `approve`需选定`vessel_name`与计划时段`planned_start`/`planned_end`（ISO8601，需覆盖预计抢修时长），系统在同一事务内核对该船在修安排与剩余备缆；有时段重叠或备缆不足则记录停在原状态，错误与时间线`resource_blocked`事件写清被哪项抢修占住。
- `reassign`（`approved`/`mobilized`→`approved`）改派船舶与时段：原安排的资源先释放，再核对并占用新安排。
- `mobilize`按实际动员量`available_spare_km`调整占用；`splice`按实际用量`spare_used_km`转为消耗。
- `restore`/`cancel`后未用备缆自动归还；记录列表与详情的`resource`块展示船、计划时段、预留、消耗、恢复容量与船舶剩余容量。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、资源占用（双重告警冲突、改派释放、按量扣减与归还）、重复引用、权限拒绝和版本冲突。
