# NasManager 中间层

绿联 UGOS Pro 的 iOS 管理端后端。替 iPhone 跟 UGOS 的加密接口打交道，对 App 输出稳定契约。

## 快速开始

```bash
# 1. 配置（项目根目录 .env）
cd <项目根目录>
# 需要这些键：
#   UGOS_HOST / UGOS_PORT / UGOS_USER / UGOS_PASSWORD
#   API_TOKEN（App 访问中间层用的令牌，非 NAS 密码）
#   SERVER_PORT / POLL_INTERVAL

# 2. 本地跑（开发）
cd server
python -m uvicorn app.main:app --host 127.0.0.1 --port 8787

# 3. 接口文档
#    http://127.0.0.1:8787/docs

# 4. 容器部署（NAS 上）
cd server
docker compose up -d --build
docker logs -f nasmanager
```

## 鉴权

除 `/health` 外，所有接口需要：

```
Authorization: Bearer <API_TOKEN>
```

失败累计 10 次封禁来源 IP 300 秒，默认限流 240 次/分钟。

## 接口清单

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 无需鉴权，供外部 uptime 监控 |
| GET | `/api/v1/capabilities` | 能力探测，接口不可用时对应字段为 false |
| GET | `/api/v1/overview` | **总览聚合**：设备 / CPU / 内存 / 风扇 / 网口 / IO / 存储摘要 / 容器统计 / 告警摘要 |
| GET | `/api/v1/system` | 硬件与固件详情 |
| GET | `/api/v1/live` | 实时监控（缓存 2 秒） |
| GET | `/api/v1/storage` | 磁盘 / 存储池 / 卷 |
| GET | `/api/v1/containers` | 容器列表与运行统计 |
| POST | `/api/v1/containers/{id}/{action}` | action = `start` / `stop` / `restart` |
| GET | `/api/v1/containers/{id}/logs?tail=200` | 容器日志 |
| GET | `/api/v1/logs?page=1&size=50&level=&module=&keyword=` | 系统日志 |
| GET | `/api/v1/alerts` | 当前活动告警 |
| GET | `/api/v1/alerts/history?limit=100` | 告警状态变化历史 |
| POST | `/api/v1/alerts/poll` | 手动触发一次巡检（联调） |
| POST | `/api/v1/alerts/containers/{name}/suppress?enabled=true` | 用户主动停的容器不再告警 |

响应统一为：

```json
{"ok": true, "ts": 1792943647, "data": { ... }}
```

错误：`{"ok": false, "ts": ..., "error": {"code": 1024, "message": "...", "path": "..."}}`

## 告警规则

| 规则 | 默认阈值 | 级别 |
|---|---|---|
| 磁盘温度过高 | 50°C | warning（≥58 升 critical） |
| CPU 温度过高 | 80°C | critical |
| 卷使用率过高 | 90% | warning（≥97 升 critical） |
| 存储池状态异常 | status ≠ 0 | critical |
| 存储池无冗余 | RAID0 / Basic / JBOD | info |
| CPU 高负载 | 95% | warning |
| 内存占用过高 | 92% | warning |
| 机箱风扇停转 | speed = 0 | warning |
| 容器意外停止 | running → 非 running | warning |

阈值全部在 `.env` 里可调（`ALERT_DISK_TEMP` 等）。

告警引擎带状态机：**只在「新出现」和「恢复」两个时刻产生事件**，不会重复刷屏。容器告警首轮只建基线，避免服务启动时误报。

## 缓存策略

| 数据 | TTL |
|---|---|
| 实时监控 | 2 秒 |
| 容器列表 | 5 秒 |
| 存储 | 30 秒 |
| 系统硬件 | 300 秒 |

## 已知限制

1. **`volume.read_rate` / `write_rate` 单位存疑** —— 实测出现过 `1.08e12` 这类量级读数，怀疑是采样窗口内的累计字节数而非 B/s。App 端需要做平滑或裁剪，进一步需对照 UGOS 前端确认。
2. **容器日志参数未完全确认** —— `ShowContainerLogs` 的请求体字段还在试，当前同时塞了 `containerId` / `ContainerId` / `tail` / `Tail` 兜底。
3. **SMART 明细未接入** —— `v1/storage/disk/smart/info` 需要磁盘参数，尚未确定字段。
4. **APNs 推送未实现** —— 告警引擎已产出事件流，推送通道是下一步。
5. 磁盘 `status` 语义：实测 `0` 与 `1` 均为正常（1 = 已激活在用），其余才告警。
