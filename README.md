# 跨海光缆故障与抢修协调

纯Python标准库实现的跨海光缆故障与抢修协调原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、海况、船机许可、备缆窗口、接续质量、船舶时段冲突和资源占用规则。
- `src/repository.py`：SQLite建表（记录、审计、船舶、资源占用）、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

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
- `GET /api/vessels`：船舶列表，含剩余备缆与进行中的占用（记录、时段、预留量）。
- `GET /api/vessels/{name}`：船舶详情，含全部占用历史。
- `POST /api/vessels`：登记船舶，请求体为`{"name":"CS-1","spare_cable_km":40}`。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

## 资源占用流程

- 审批（`approve`）必须选定`vessel_name`与计划时段`planned_start`/`planned_end`（ISO时间）。系统核对该船同时段的已有安排和剩余备缆：时段重叠或备缆不足即返回409，并写清被哪条记录的抢修占住，当前记录停在原状态。
- 在`approved`状态再次`approve`即改派：原安排的船舶时段与预留备缆先释放，再按新安排重新核对占用。
- 动员（`mobilize`）按实际装船备缆`available_spare_km`多退少补；接续（`splice`）按实际消耗`spare_used_km`核销，消耗不得超过船上备缆。
- 取消（`cancel`）或恢复（`restore`）后，未消耗备缆自动归还船舶库存。
- 记录详情与列表的`payload`中可见`vessel_name`、`planned_start`/`planned_end`、`reserved_spare_km`、`onboard_spare_km`、`spare_used_km`与最终容量`restore_capacity_gbps`；审计时间线的`details.resource`记录每次扣减与归还。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
