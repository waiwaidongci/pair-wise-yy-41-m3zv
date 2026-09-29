# 桥梁结构监测与限行决策

融合传感、巡检、交通荷载和天气数据，生成限载限行或恢复建议。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/cable.py`：斜拉索索力缓变识别（同温度段基线、连续超限、待核判定、通告有效期）。
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
- `POST /api/items/{id}/records/{rid}/close`，解除（关闭）记录，如解除交通通告
- `POST /api/items/{id}/readings` / `GET /api/items/{id}/readings`，斜拉索读数
- `POST /api/items/{id}/reviews` / `GET /api/items/{id}/reviews`，人工复核
- `POST /api/items/{id}/transition`，必须提交`expected_version`；斜拉索测点限行/封闭还需`notice_ref`
- `GET /api/audit`

允许角色：sensor_operator, bridge_engineer, traffic_authority, viewer。监测偏差与预警阈值之比和多条异常记录决定告警等级；限行与封闭决策必须绑定交通通告记录。

## 斜拉索索力缓变识别

夜间温差大时索力随温度缓变，为避免单跳点直接把值班台推到封闭，创建测点时传
`"item_type":"cable_point"`（测点阈值 `threshold` 即扣减后索力限值），随后按笔提交
索力、温度和采集时刻：

- `POST /api/items/{id}/readings`（sensor_operator）：提交 `force`、`temperature`、
  `read_at`，可选 `offline`。
  - 先扣**同温度段基线**：温度按 5°C 分段，基线取该段历史正常读数（当时扣减后未
    超限）的原始索力均值；该段首笔冷启动，以自身为种子、偏差记0。响应中
    `baseline/baseline_samples/baseline_method/corrected_force/deduction` 给出扣减依据。
  - **连续三笔**已采纳读数扣减后仍高于限值，测点才自动进入严重告警 `warning`；
    中间任一笔恢复即重新计数，单笔跳点不改变建议。
  - 传感器离线（`offline=true` 或缺 `force`）、采集时刻早于或等于已采纳读数
    （时间倒置）、温度缺失的读数只标记为 `pending`（`pending_reasons` 列出全部
    原因），不参与基线和连续计数，也不改变当前建议。
- `GET /api/items/{id}/readings`：换班视图，保留每笔原读数、待核原因与扣减依据。
- `POST /api/items/{id}/reviews`（bridge_engineer）：人工复核。**必须换人**——
  复核人不能是触发严重告警的提交人；`anomaly:false` 时保留原状态，
  `anomaly:true` 确认异常后才允许关联交通通告。
- 交通通告为 `kind=traffic_notice` 的记录（仅 traffic_authority 可创建，
  可选 `valid_from/valid_until` 有效期）。限行（restricted）与封闭（closed）转换
  必须在请求体中带 `notice_ref` 且通告当前有效；通告缺失、不存在、已解除
  （`POST /api/items/{id}/records/{record_id}/close`）或已过有效期时，
  返回 409 并保留原状态、说明原因。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
