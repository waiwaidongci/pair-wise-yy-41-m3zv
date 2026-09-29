# 桥梁结构监测与限行决策

融合传感、巡检、交通荷载和天气数据，生成限载限行或恢复建议。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8318
```

默认端口为`8318`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET/POST /api/items/{id}/baselines`
- `GET/POST /api/items/{id}/readings`
- `POST /api/items/{id}/alarms/{alarm_id}/review`
- `POST /api/items/{id}/traffic-notices`、`POST /api/items/{id}/traffic-notices/{record_id}/lift`
- `GET /api/audit`

允许角色：sensor_operator, bridge_engineer, traffic_authority, viewer。监测偏差与预警阈值之比和多条异常记录决定告警等级；限行与封闭决策必须绑定交通通告记录。

## 斜拉索索力缓变识别

- `POST /api/items/{id}/baselines`：配置同温度段基线（左闭右开 `tmin<=温度<tmax`，`tmax`可空表示无上界）。角色：sensor_operator/bridge_engineer。
- `POST /api/items/{id}/readings`：测点提交索力`force`、温度`temperature`、采集时刻`measured_at`（ISO 8601）。处理顺序：
  1. `offline=true` 传感器离线；
  2. 温度缺失；
  3. 采集时刻早于最近一笔有效读数（时间倒置）；
  4. 无匹配温度段基线。

  以上四类读数一律记为`pending`只留待核，**不参与连续计数、不改变当前建议**。
  有效读数先扣同温度段基线（`force_adjusted = force - baseline_force`）；**连续三笔**扣减后仍高于限值（`threshold`，严格大于）才自动置`severity=critical`并生成严重告警（单次跳点不会触发）。
- `GET /api/items/{id}/readings?status=pending|accepted`：换班后可查原读数（`force`/`temperature`/`measured_at_raw`）与扣减依据（温度段、基线值、调整值），告警证据含三笔原始读数。
- `POST /api/items/{id}/alarms/{alarm_id}/review`：人工复核，角色`bridge_engineer`且**必须换人**（不能是提报人），需带`expected_version`与`note`；`decision=confirmed`确认异常后状态`normal→warning`，`rejected`保留原状态并说明原因（被拒批次不再计入连续计数）。
- `POST /api/items/{id}/traffic-notices` / `POST /api/items/{id}/traffic-notices/{record_id}/lift`：交通通告登记/解除，角色`traffic_authority`。状态迁移到`closed`时必须存在未解除的有效通告；通告缺失或已解除返回409、保留原状态，原因写入审计（`transition_blocked`）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
