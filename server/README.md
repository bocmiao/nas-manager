# NasManager 中间层

绿联 UGOS Pro 的 iOS 管理端后端。替 iPhone 跟 UGOS 的私有加密接口打交道，对 App 输出稳定契约。

已验证固件：**UGOS Pro 1.19.1.0126**（DXP4800 Plus）。接口契约见 [`../docs/api-verified.md`](../docs/api-verified.md)。

---

## 快速开始

```bash
# 1. 配置（项目根目录 .env，参考 .env.example）
#    必需：UGOS_HOST / UGOS_PORT / UGOS_USER / UGOS_PASSWORD
#    建议：API_TOKEN（App 访问中间层用的令牌，非 NAS 密码）

# 2. 本地开发
cd server
python -m uvicorn app.main:app --host 127.0.0.1 --port 8787

# 3. 接口文档（自动生成）
#    http://127.0.0.1:8787/docs

# 4. 容器部署
cd server && docker compose up -d --build && docker logs -f nasmanager
```

> ⚠️ 如果本机/容器环境里有 `http_proxy` / `HTTPS_PROXY`，访问 NAS 与 `127.0.0.1` 时要绕开：
> `NO_PROXY=127.0.0.1,<NAS 地址>`。NAS 走直连，不要过代理。

---

## 鉴权

除 `/health` 外所有接口需要：

```
Authorization: Bearer <API_TOKEN>
```

失败累计 10 次封禁来源 IP 300 秒，默认限流 240 次/分钟。

响应统一：

```json
{"ok": true, "ts": 1792943647, "data": { ... }}
```

错误：`{"ok": false, "ts": ..., "error": {"code": 1024, "message": "...", "path": "..."}}`
`code` 是 UGOS 返回码（见契约文档错误码表），不是 HTTP 状态码；HTTP 状态为 502/503。

---

## 接口清单

### 公开

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 无需鉴权。含 `ugos.reachable` / `authenticated` / `last_poll_ok` / 推送状态，供外部 uptime 监控 |

### 系统与总览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/capabilities` | 能力探测，接口不可用时对应字段为 `false` |
| GET | `/api/v1/overview` | 总览聚合：设备 / CPU / 内存 / 风扇 / 网口 / IO / 存储 / SMART 摘要 / 容器统计 / 告警 |
| GET | `/api/v1/system` | 硬件与固件详情（含 UPS、网口协商速率） |
| GET | `/api/v1/live` | 实时监控（2 秒缓存） |
| GET | `/api/v1/storage` | 磁盘 / 存储池 / 卷 |

### 存储 · SMART / 测速

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/storage/disks/{disk}/smart` | **SMART 明细**（关键项 + 全部属性 + 自检状态） |
| GET | `/api/v1/storage/disks/{disk}/state` | 磁盘运行状态、温度、重连/重扫次数、按月坏道 |
| POST | `/api/v1/storage/disks/{disk}/benchmark` | **发起测速**，202 + 任务对象，立刻返回 |
| GET | `/api/v1/storage/disks/{disk}/benchmark` | 查当前任务状态与进度 |
| DELETE | `/api/v1/storage/disks/{disk}/benchmark` | 停止测速 |
| GET | `/api/v1/storage/disks/{disk}/benchmark/history` | 历史测速记录 |

`{disk}` 用**盘名**（`sda`）、**标签**（`硬盘1`）或**序列号**均可。
带斜杠的 `/dev/sda` 不能放进路径（会被当成多级路由），请用盘名。

### 容器 / Docker

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/containers` | 容器列表（含 `restarting` / cpu / mem / 端口映射） |
| POST | `/api/v1/containers/{id}/{action}` | action = `start` / `stop` / `restart` |
| GET | `/api/v1/containers/{id}/logs?tail=200&keyword=` | **容器日志**，已解 Docker 多路复用帧 |
| GET | `/api/v1/docker/usage` | **Docker 资源占用**：引擎状态 + 每容器 CPU/内存/网络 |

### 任务管理器

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/processes?limit=50&sort=cpu\|mem` | 进程列表（只读，含每进程 CPU/内存） |
| GET | `/api/v1/services` | 系统服务列表（只读） |

### 日志 / 告警 / 推送

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/logs?page=1&size=50&level=&module=&keyword=` | 系统日志分页 |
| GET | `/api/v1/alerts` | 当前活动告警 + 摘要 |
| GET | `/api/v1/alerts/history?limit=100` | 告警状态跳变历史 |
| GET | `/api/v1/alerts/rules` | 当前生效的 15 条规则与阈值 |
| POST | `/api/v1/alerts/poll` | 手动触发巡检（联调），返回事件与推送成功数 |
| POST | `/api/v1/alerts/containers/{name}/suppress?enabled=true` | 用户主动停的容器不再告警 |
| GET | `/api/v1/push/devices` | 已注册推送设备 + 通道统计 |
| POST | `/api/v1/push/devices` | 注册设备 `{"token","platform","name"}` |
| DELETE | `/api/v1/push/devices/{token}` | 注销设备 |
| POST | `/api/v1/push/test` | 发一条测试推送 |

---

## 磁盘 SMART

`v1/storage/disk/smart/info` 的 `disk` 参数**必须传 `dev_name`（`/dev/sda`）**。
实测：传裸盘名 `sda` 返回 `5001 硬盘不存在`，不传返回 `9999`，传 `dev_name` 才通。
中间层对外收盘名/标签/序列号，内部自动换成 `dev_name`。

归一化后的关键项来自 ATA 属性 ID：

| 字段 | 属性 ID | 含义 |
|---|---|---|
| `power_on_hours` | 9 | 通电时长（小时） |
| `temperature` | 194 | 温度 |
| `reallocated_sectors` | 5 | 重映射扇区数 |
| `pending_sectors` | 197 | 待映射扇区 |
| `offline_uncorrectable` | 198 | 脱机不可校正 |
| `seek_error_rate` | 7 | 寻道错误率 |
| `read_error_rate` | 1 | 底层读取错误率 |
| `udma_crc_errors` | 199 | 接口 CRC 错误（多为线缆/背板） |

`health` 由属性 `status` 推导：全部为 1 → `ok`；1~2 条异常 → `warning`；≥3 条 → `failed`。

## 磁盘测速

- 接口：`benchmark/start`（POST body `{"disk": "/dev/sdX"}`）、`benchmark/running`、`benchmark/info`、`benchmark/stop`
- **写操作需要二次密码校验**：请求头 `X-CurPass-Token`，值是 RSA 加密后的账号密码。
  缺这个头返回 `1101 密码验证失败`。中间层在 `UgosClient.post_privileged()` 里自动生成并缓存，遇到 1101 会刷新后重试一次。
- 接口是「发起 + 轮询」模型，中间层包成本地异步任务：`POST` 立刻返回 202，后台协程每 3 秒查一次进度，前端轮询 `GET` 即可。
- **在用磁盘不能测速**：NAS 返回 `9999` + `debug="disk is busy: 1"`，中间层转成 HTTP 409 并附中文 `hint`。
- 可用 `BENCHMARK_ALLOW_START=false` 在服务端整体关掉这个能力。

## 容器日志

- `ShowContainerLogs` 是 POST，请求体 `{"containerId": ..., "tail": N}`（`ContainerId`/`Tail` 大写也能过校验）。
- 返回的是 **Docker 多路复用流**：每帧 8 字节头（1 字节流类型 + 3 字节保留 + 4 字节大端载荷长度）。
  帧头长度按 **UTF-8 字节**计，而响应体已被服务端解码成字符串，所以解帧时必须按字符累加其 UTF-8 字节数对齐，不能直接用字符下标。
- **服务端忽略行数参数**：传任何 `tail` 都返回完整日志，所以 `tail` 在中间层本地截断，
  响应里 `total_lines` 是真实总行数、`truncated` 表示是否被截断。

## Docker 资源占用

- `ShowCpu` / `ShowMemory` 实测是**宿主机级**（传 `containerId` 也不生效，返回整机 CPU 信息和内存统计），不能当容器资源用。
- 按容器的数据在 `docker/view/ObtainOverviewInfo` 的 `overviewContainers`：
  `cpuUsagePercentage` / `memoryUsagePercentage`（实为字节）/ `upstreamTraffic` / `downstreamTraffic`。
- 该接口**只返回运行中的容器**（本机 16 个容器里返回 9 个）。需要全量请配合 `/containers`。
- `net_*_bytes` 是累计字节数，不是速率。

## 任务管理器

`v1/taskmgr/processes`、`v1/taskmgr/services` 均可用，返回每项的 `consume`（CPU/内存/网络/磁盘/GPU）。
中间层只做**只读**接入：进程/服务的 `stop`、`restart` 属高危写操作，需要二次密码校验，暂不暴露。

---

## 告警规则（15 条）

| # | 规则 key | 级别 | 默认阈值 | 类型 |
|---|---|---|---|---|
| 1 | `disk_temp` | warning | ≥50°C（≥58 升 critical） | 绝对 |
| 2 | `disk_status` | critical | status ∉ {0,1} | 绝对 |
| 3 | `disk_smart` | warning | 重映射/待映射/脱机不可校正 ≥1 | 绝对 |
| 4 | `disk_smart_crc` | info | CRC 错误 >0 | 绝对 |
| 5 | `pool_status` | critical | status ≠ 0 | 绝对 |
| 6 | `pool_no_redundancy` | info | RAID0 / Basic / JBOD | 绝对 |
| 7 | `volume_used` | warning | ≥90%（≥97 升 critical） | 绝对 |
| 8 | `cpu_temp` | critical | ≥80°C | 绝对 |
| 9 | `cpu_used` | warning | ≥95% | 绝对 |
| 10 | `mem_used` | warning | ≥92% | 绝对 |
| 11 | `fan_stall` | warning | 状态=启用且转速 <100 RPM | 绝对 |
| 12 | `ups_on_battery` | critical | 市电中断 | 绝对 |
| 13 | `container_down` | warning | 连续 2 轮观察到非运行 | 跳变 |
| 14 | `container_restart_loop` | warning | 10 分钟内重启 ≥3 次，或处于重启退避 | 跳变 |
| 15 | `nic_speed` | warning | 低于历史峰值，或低于 `ALERT_NIC_MIN_MBPS` | 跳变 |

阈值全部在 `.env` 里可调。

**状态机语义**

- 只在 `raised`（新出现）和 `resolved`（恢复）两个时刻产生事件，持续异常不重复发。
- **绝对型规则**首轮即按真实值判定（磁盘 60°C 就是 60°C，没有「基线」可言）。
- **跳变型规则**首轮只建基线：网口峰值速率、容器运行状态、重启次数都要有上一轮样本才有意义，
  否则服务重启时会把一堆历史状态当成新故障。
- `container_down` 需要连续两轮确认，避免「重启循环里恰好被采样到停止」刷屏；
  启动前就已停止的容器不会计入（只在实际发生 running→stopped 跳变时开始计数）。

**风扇规则的两个实测细节**

- `status` 语义：`1` = 风扇在转，`0` = 未启用/无此风扇。本机 `cpu_fan` 恒为 `speed=0,status=0`（实际无此风扇），
  所以只对 `status==1` 且转速过低的风扇告警，否则整机永久误报。
- 机箱风扇正常值参考：544~576 RPM。

---

## APNs 推送

### 配置

四项齐全才启用，缺一项就降级为「只写日志」：

```
APNS_KEY_FILE=/app/secrets/AuthKey_XXXXXXXXXX.p8   # Apple 后台下载的 ES256 私钥
APNS_KEY_ID=XXXXXXXXXX                             # Key ID
APNS_TEAM_ID=YYYYYYYYYY                            # 开发者 Team ID
APNS_TOPIC=com.example.nasmanager                  # App Bundle ID
APNS_USE_SANDBOX=false                             # 调试包改 true
```

### 行为

- 设备注册表在**内存**里，进程重启即清空 —— App 每次启动重新 `POST /api/v1/push/devices` 即可。
- 推送直连 `api.push.apple.com`（或 sandbox 域名），HTTP/2，JWT 用 ES256 签名（`cryptography` 现签，不需要 PyJWT）。
- JWT 缓存 50 分钟（Apple 要求 20~60 分钟内轮换）。
- 同一告警 key 有 10 分钟去重窗口（`APNS_PUSH_DEDUP_SECONDS`），防止服务重启后重复轰炸。
- 收到 `410 Unregistered` / `BadDeviceToken` 会自动把设备从注册表剔除。
- **凭据没配、缺 `h2` 依赖、网络不通，一律只写日志并返回 0，绝不抛异常影响巡检。**
- `critical` 级别用 `interruption-level: time-sensitive` + priority 10，其余 `active` + priority 5。

HTTP/2 依赖 `h2` 包（已写进 `requirements.txt`）。

---

## 轻量哨兵 `scripts/sentinel.py`

中间层跑在 NAS 自己身上，NAS 整机挂了它也一起消失 —— 这种场景它自己报不出来。
哨兵是个**单文件、只用标准库**的小脚本，跑在别处（另一台机器 / 路由器 / 树莓派 / cron），从外部盯着。

```bash
# 盯中间层 + NAS 端口，异常时打 webhook
python sentinel.py \
  --url http://192.0.2.10:8787/health \
  --nas 192.0.2.10:9443 \
  --webhook https://example.com/hook \
  --token "$API_TOKEN" \
  --interval 60 --retries 3

# 只跑一轮（配合 cron）
python sentinel.py --url ... --webhook ... --once
```

- 参数都能用 `SENTINEL_*` 环境变量给（`--url` → `SENTINEL_URL`），命令行优先。
- 检查三层：`/health` 可达 → `ugos.reachable` / `authenticated` 为真 → 带 token 时额外打一个需鉴权接口确认 token 还有效。
- `--nas host:port` 额外做一次 TCP + TLS 握手，用来区分「中间层挂了」和「整机挂了」。
- 只在**状态跳变**时发 webhook（故障发一次、恢复发一次），持续异常不重复。状态存 `--state-file`。
- webhook 支持 `json`（默认）和 `text` 两种体格式，`--webhook-header K:V` 可加鉴权头。
- 退出码：0 正常，2 参数错误，130 被中断。

---

## 缓存策略

| 数据 | TTL |
|---|---|
| 实时监控 | 2 秒 |
| 容器列表 | 5 秒 |
| 存储 | 30 秒 |
| SMART | 60 秒 |
| 系统硬件 | 300 秒 |
| 能力探测 | 60 秒 |

---

## 部署

### 本机

```bash
cd server
NO_PROXY=127.0.0.1,192.0.2.10 python -m uvicorn app.main:app --host 127.0.0.1 --port 8787
```

### Docker（NAS 上）

```bash
cd server
docker compose up -d --build
```

compose 用 `env_file: ../.env`，容器内访问宿主机 NAS 用宿主 IP。
`restart: unless-stopped` + `HEALTHCHECK` 已在 compose 里配好。

APNs 私钥请挂载进容器并设置 `APNS_KEY_FILE`，不要把 `.p8` 打进镜像。

### 外部兜底

中间层自己挂了没人发告警，所以**必须**配一个外部监控：

- `GET /health` 每 30~60 秒一次
- 或直接跑 `scripts/sentinel.py`，它会把「NAS 挂了」也一起报出来

---

## 已知限制

1. **磁盘测速在在用磁盘上跑不了** —— 测试机两块盘都在 RAID0 存储池里，NAS 直接拒绝
   （`9999 / disk is busy: 1`）。接口、密码令牌、异步任务框架都已打通并实测，
   但「测速跑完拿到 MB/s 数字」这一步需要一块空闲盘才能验证。
2. **UPS 字段未经真机验证** —— 本机未接 UPS，`sysinfo/machine/common` 的 `hardware.ups` 恒为 `null`。
   `_check_ups` 按常见命名做多路兜底（`on_battery` / `power_status` / `status` 等），
   取不到就**不告警**（宁漏勿错）。接上 UPS 后需要用真实返回校准一次。
3. **`volume.read_rate` / `write_rate` 单位存疑** —— 实测出现过 `1.08e12` 量级，
   怀疑是采样窗口内的累计字节数而非 B/s。App 端请做平滑或裁剪。
4. **容器日志无法区分 stdout / stderr** —— 解帧后流类型已丢失，`stream` 参数未启用。
5. **容器日志行数参数服务端不生效** —— 一律返回全量，`tail` 由本地截断；超大日志（100KB+）每次都要整包拉。
6. **Docker 资源接口只返回运行中的容器** —— 已停止容器没有 CPU/内存数据（本来也没有）。
7. **进程/服务的启停未接入** —— 属需要二次密码校验的高危写操作，本期只做只读。
8. **APNs 只用假凭据验证到 403** —— 真实推送到设备需要有效 `.p8` + 真机 token；
   JWT 结构、ES256 签名、HTTP/2 链路、请求头均已在本地验签 + 真实打到 Apple 端点确认。
9. 磁盘 `status` 语义：`0` 与 `1` 均为正常（`1` = 已激活在用），其余才告警。
10. `v1/idx/info` 在本机返回 `8003`，属该接口自身问题，与认证无关。
