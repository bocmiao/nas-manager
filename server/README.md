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
| GET | `/api/v1/firmware` | **固件版本查询**：当前版本 / 可用新版本 / 是否有更新（只读，不含升级） |
| GET | `/api/v1/ups` | **UPS 状态**：未接时返回 `supported: false` |
| GET | `/api/v1/history?metric=cpu&points=120` | **历史曲线**（内存环形缓冲，60s 一点 / 6 小时） |
| GET | `/api/v1/history/metrics` | 可用指标清单与缓冲状态（`since` / `samples` / `last_error`） |

### 用户与共享（只读）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/users` | **用户列表**：UID / 用户名 / 角色 / 是否管理员 / 所属组 / 最后登录时间 |
| GET | `/api/v1/users/{uid}` | 用户详情：共享权限、配额、应用授权 |
| GET | `/api/v1/groups` | **用户组列表**：自定义组 + 内置组（成员、共享权限） |
| GET | `/api/v1/shares` | 共享文件夹与权限掩码 |

> 只读。创建/删除用户、改密码、改权限都是高危写操作，中间层不实现。

### 网络

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/network/interfaces` | **网口详情**：速率 / 双工 / MTU / 瞬时速率 / 累计流量估算 |
| GET | `/api/v1/network/interfaces/{name}` | 单网口，例如 `eth0` |

### 存储 · SMART / 测速

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/storage/disks/{disk}/smart` | **SMART 明细**（关键项 + 全部属性 + 自检状态） |
| GET | `/api/v1/storage/disks/{disk}/state` | 磁盘运行状态、温度、重连/重扫次数、按月坏道 |
| POST | `/api/v1/storage/disks/{disk}/benchmark` | **发起测速**，202 + 任务对象，立刻返回 |
| GET | `/api/v1/storage/disks/{disk}/benchmark` | 查当前任务状态与进度 |
| DELETE | `/api/v1/storage/disks/{disk}/benchmark` | 停止测速 |
| GET | `/api/v1/storage/disks/{disk}/benchmark/history` | 历史测速记录 |
| GET | `/api/v1/storage/health` | **存储健康汇总**：池 RAID 重建/校验进度、卷容量、inode 支持情况 |
| GET | `/api/v1/storage/disk-sleep` | **硬盘休眠状态** + 休眠策略 + 频繁唤醒统计 |

`{disk}` 用**盘名**（`sda`）、**标签**（`硬盘1`）或**序列号**均可。
带斜杠的 `/dev/sda` 不能放进路径（会被当成多级路由），请用盘名。

### 容器 / Docker

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/containers` | 容器列表（含 `restarting` / cpu / mem / 端口映射） |
| GET | `/api/v1/containers/baseline` | **容器「期望运行」基线**（连续 N 轮 running 的容器） |
| GET | `/api/v1/containers/{id}/detail` | **容器详情**：环境变量 / 端口映射 / 卷 / 网络 / 资源限制（`compose` 恒为不可用） |
| POST | `/api/v1/containers/{id}/{action}` | action = `start` / `stop` / `restart` |
| GET | `/api/v1/containers/{id}/logs?tail=200&keyword=` | **容器日志**，已解 Docker 多路复用帧 |
| GET | `/api/v1/docker/usage` | **Docker 资源占用**：引擎状态 + 每容器 CPU/内存/网络 |

### Docker 镜像

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/docker/images` | **镜像列表**：标签 / 大小 / 创建时间 / 被哪些容器引用 |
| DELETE | `/api/v1/docker/images?ids=a&ids=b` | **删除镜像**，1 个走单个接口、多个自动走批量接口。`ids` 可用 `sha256:…` / `name:tag` / 镜像名 |
| POST | `/api/v1/docker/images/pull` | **拉取镜像**，202 + 任务对象，body `{"name":"nginx","tag":"latest"}` |
| GET | `/api/v1/docker/images/pull?ref=nginx:latest` | 查拉取任务状态与进度 |
| GET | `/api/v1/docker/images/pulls` | 本进程发起的所有拉取任务 |

### 快照

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/snapshots` | **快照总览**：能力判定、卷文件系统、各类型的快照文件夹 |
| GET | `/api/v1/snapshots/{folder_type}` | 某类型的快照文件夹，`folder_type` = `share` \| `home` |
| GET | `/api/v1/snapshots/{folder_type}/{folder_id}?page=1&size=50` | 该文件夹的快照列表 |
| GET | `/api/v1/snapshots/{folder_type}/{folder_id}/schedule` | 快照计划 + 保留策略（只读） |
| POST | `/api/v1/snapshots/{folder_type}/{folder_id}` | 立即创建快照，body `{"comment": "..."}` 可空 |
| DELETE | `/api/v1/snapshots/{folder_type}/{folder_id}?names=a&names=b` | 删除快照（批量） |
| POST | `/api/v1/snapshots/{folder_type}/{folder_id}/restore` | **回滚**，body 必须带 `{"name":"<快照名>","confirm":"ROLLBACK"}` |

> 回滚会覆盖当前数据且不可撤销，`confirm` 不等于 `ROLLBACK` 一律 400 拒绝，不会默认执行。

### 任务管理器

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/processes?limit=50&sort=cpu\|mem` | 进程列表（只读，含每进程 CPU/内存） |
| GET | `/api/v1/services` | 系统服务列表（只读） |
| GET | `/api/v1/services/health` | **关键服务健康**：UGOS 服务 + 系统守护进程 + Docker 引擎 + 服务基线 |

### 日志 / 告警 / 推送

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/logs?page=1&size=50&level=&module=&keyword=` | 系统日志分页 |
| GET | `/api/v1/alerts` | 当前活动告警 + 摘要 |
| GET | `/api/v1/alerts/history?limit=100` | 告警状态跳变历史 |
| GET | `/api/v1/alerts/rules` | 当前生效的 20 条规则与阈值（第三批新增的 5 条带 `available` / `reason`，标明真机能否取到数据） |
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

## 镜像管理

- 端点全部是 **POST**：`ShowLocalImageV2`（本地镜像）、`ObtainAllImages`（Docker Hub 商店搜索，
  与本地无关）、`DeleteImage` / `BatchDeleteImage`、`DownloadImage`。用 GET 访问一律 `9405`。
- **拉取没有字节级进度**。UGOS 的做法是：拉取期间镜像列表里出现一条
  `<name>:<tag>:pull`、`status=2` 的临时条目，完成后变成 `status=1` 并带上真实 `imageSize`。
  `progress` 字段实测恒为 0，所以中间层只能报「进行中 / 完成 / 失败」，不编造百分比。
- 中间层把拉取包成异步任务：`POST /docker/images/pull` 立刻返回 202，后台每 3 秒轮询一次列表，
  客户端轮询 `GET /docker/images/pull?ref=` 拿状态。镜像已存在时直接返回 `succeeded` 并注明未重复拉取。
- **删除要自己校验存在性**：UGOS 对不存在的镜像同样返回 `successful`，所以中间层先列一次做校验，
  响应里 `deleted` / `missing` 分列。`BatchDeleteImage` 的 `imageIds` 必须是**对象数组**
  （`{id,status,imageName,imageVersion}`），传字符串数组会报 `cannot unmarshal string ...`。
- 「关联容器数」用 `QueryAllContaners`（body `{"list":[{imageId,imageRef}]}`）一次批量查，
  返回 `{result:[{imageId,names:[容器名]}]}`；没有容器引用时返回 `{}`。

## 快照

「快照」是独立的 `com.ugreen.snapshot` 应用（entry `/snapshot`），**不在存储管理里** ——
storagemgr 的 bundle 里只有 `snapshot_size` 这种空间占用字段，没有任何快照接口。

- 快照以「文件夹」为粒度：`folder_type` 只有 `share`（共享文件夹）与 `home`（家目录）。
- 列表接口 `v1/snapshot/snapshot` 与文件夹接口都强制要求 `page_number` / `page_size`，
  缺了返回 `9999` + `limit is 0, offset: 0, limit: 0`。
- **只支持 Btrfs**：本机 volume1 是 ext4，创建/回滚直接被拒
  （`9999` + `folderServ.GetBaseFolder(): mnt point of home is not btrfs filesystem`），
  列表返回 `32003 文件夹不存在`。中间层把这些错误翻成 `supported:false` + `reason`。
- 删除接口对不存在的快照也返回 `success`，所以响应里带 `verified`：只有重新列到列表才能确认真的删掉了。
- 中间层不提供「修改快照计划」的写接口，避免误改用户备份策略；`schedule` 只读。

## 固件检查

- `firmware/status` 的 `current_version` 是字符串（`1.19.1.0126`），
  `firmware/version/is_new` 里同名字段是整数（`119010126`），**不能混用**。
- 中间层只做查询：`current_version` / `latest_version` / `has_update` / `size_bytes` / `notes`（更新说明）。
- `state`、`operate_type`（本机 2 / 5）语义未确认，原样透出，不做猜测性解释。
- **不实现升级动作**：固件升级不可逆，App 端只做提醒。

## UPS

- `sysinfo/machine/common` 的 `hardware.ups` 在未接 UPS 时恒为 `null` → `supported:false, connected:false`。
- 已接时按常见命名多键兜底取 `battery_percent` / `runtime_seconds` / `on_battery` / `events`，
  同时整包透出 `raw`，并标 `verified:false`（本机无法验证字段名）。

## 历史采样

- 进程内环形缓冲，默认 **60 秒一采、每指标 360 点（6 小时）**，
  可用 `HISTORY_INTERVAL` / `HISTORY_POINTS` / `HISTORY_ENABLED` 调整。
- 指标：`cpu` / `mem` / `cpu_temp` / `fan`（机箱风扇 RPM）/ `net_tx` / `net_rx` / `disk_temp`（按盘）。
- 查询：`/history?metric=cpu&points=120`，返回 `{metric, unit, since, interval, stale, points:[{ts,v}]}`。
  `disk_temp` 不带 `disk` 时返回所有盘的序列，带 `disk=硬盘1` 时只返回该盘。
- **重启即清空**：`since` 是缓冲里最早一个点的真实时间戳，不是固定「6 小时前」，客户端按它画起点。
  `stale=true` 表示超过 3 个采样周期没有新点（采样协程卡住或 NAS 不可达）。

## 任务管理器

`v1/taskmgr/processes`、`v1/taskmgr/services` 均可用，返回每项的 `consume`（CPU/内存/网络/磁盘/GPU）。
中间层只做**只读**接入：进程/服务的 `stop`、`restart` 属高危写操作，需要二次密码校验，暂不暴露。

## 用户与权限（只读）

端点全部在**控制面板 `/ctlmgr`** 的入口 bundle 里（`v1/user/*`、`v1/group/*`），没有独立的用户应用。

- `v1/user/list?reverse=false` → `{"list":[...]}`，字段含 `uid` / `username` / `role` / `groups[{gid,name}]` /
  `account_type` / `is_domain` / `is_expired` / `enable_home_dir` / `dir_quota`。
  **没有登录时间字段**，`v1/user/uid/{uid}` 也没有。
- **最后登录时间靠审计日志反查**：`v1/log/query?keyword=<用户名>` 里，
  Web/App 登录是 `module=login`、`operator=<用户名>`、`content="User [xxx] from [...] logged in successfully."`；
  SSH 登录是 `module=control_panel`、`operator=system`，用户名只在正文里。
  中间层优先取 `operator` 精确匹配，命中不到再退到正文包含，`last_login_source` 会写明来源。
- `v1/group/list?reverse=false` 本机返回**空数组**：UGOS 只列自定义组，内置组（`admin` gid=10、
  `users` gid=100、`docker` gid=121、`ughomeusers` gid=133）不出现在列表里，
  但会出现在每个用户的 `groups` 字段中。中间层据此归纳出内置组，并补上成员数与组共享权限
  （`v1/group/permission/{gid}` 对内置组有效；`v1/group/info/{gid}` 却返回 `9999 record not found`）。
- 共享权限：`v1/user/permission/{uid}`、`v1/group/permission/{gid}`、`v1/filemgr/getShareList` 都是 `result[]`。
  `permission_mask` 的位含义按 Unix `rwx`(4/2/1) 推断，**UGOS 没有官方文档**，响应里同时给原始掩码；
  组权限里出现过 `-1`，语义未知，中间层不做位解析。
- **敏感字段处理**：`otp_info.secret`、`password` 一律不返回，只给 `otp_enabled`。
- `v1/user/authorized/{uid}` 返回应用级授权（`com.ugreen.pro.usermgr` 等），原样透出。
- 写操作（`user/create`、`user/delete`、`user/edit`、`user/permission/{uid}` 等）**未实现**。

## 磁盘休眠与频繁唤醒

- **休眠状态唯一来源是磁盘列表的 `is_standby`**（布尔）：`v2/storage/disk/list` 的每块盘都有；
  实测本机两块盘 `is_standby=false`、`status=1`、温度正常上报。
- 直连查询接口**不存在**：`v1/storage/disk/sleep`、`/standby`、`/hibernate`、`/power`、`/power/state`、
  `/status` 全部 `9404`；`v1/power/*` 是 404。只有 `v1/taskmgr/stat/disk` 这类聚合里有 `status`。
- 休眠策略在 `v1/hardware/power/config`：`hard_drive_flag`（是否启用）、`hard_drive_unit` + `hard_drive_time`
  （空闲时长，本机 `M` / 20，即 20 分钟）、`hard_drive_strategy`、`hard_drive_target_info`（按池/按盘）、
  `usb_hard_drive_*`、`enable_hard_drive_sleep_log`。接口同时被用来读开机计划（`time_switches`）。
- **中间层自己不会把盘弄醒**：巡检读 SMART 时跳过 `is_standby=true` 的盘（读 SMART 需要盘转起来，
  否则休眠策略会被监控程序自身破坏）。代价是休眠期间 SMART 数据停在旧值。
- 状态枚举 `active` / `sleeping`；`standby` 无法与 `sleeping` 区分（ATA 层两种状态 UGOS 只暴露一个布尔），
  因此不编造第三个值。
- 频繁唤醒判定：`DiskSleepMonitor` 按巡检采样记录「睡 → 醒」跳变，窗口（默认 3600s）内达到阈值
  （默认 6 次）即告警。**采样粒度 = 巡检间隔**，两次采样之间的短时唤醒会漏计。
- 历史曲线：`/history?metric=disk_sleep` 按盘输出 1（休眠）/0（工作）的台阶序列。
- **实测未观察到休眠**：2026-10-26 08:18–08:43 连续 13 轮采样（120s/轮，跨 24 分钟，已越过策略的
  「空闲 20 分钟」阈值），两块盘 `is_standby` 恒为 `false`。原因是机器上常驻 9 个容器
  （qBittorrent / Lucky / 青龙 / Minecraft 等）持续产生磁盘 IO，空闲计时不断被打断。
  → 真实的「睡→醒」跳变在本环境无法自然复现，跳变计数与告警用合成数据验证
  （`scripts/test_alerts_batch3.py` 第 5 组）；状态字段本身的解析走的是真实接口。

## 容器「应有状态」

社区反馈「系统升级后容器自动重启失败」。原规则 `container_down` 只能发现「跑着跑着停了」，
且对每个容器都报，容易被重启循环刷屏。现在分三条、互斥：

1. `container_restart_loop`：窗口内停→起 ≥3 次，或处于 Docker 重启退避（`restarting=true`）
2. `container_expected_down`：连续 3 轮巡检 `running` → 打上「期望运行」标记；
   之后连续 2 轮观察到停止，且未 `suppress`、未命中重启循环，才告警
3. `container_down`：**没有**建立期望基线的容器（刚部署不到 3 轮就挂）走这条

- 基线一旦建立就保持：容器恢复运行后仍期望它继续跑；容器从列表消失（被删除）才清理基线。
- `suppress` 语义复用原有机制：`POST /alerts/containers/{name}/suppress?enabled=true` 后，
  用户主动停的容器三条规则都不发；主动停止也会把已存在的 `container_expected_down` 清掉。
- 基线可查：`GET /api/v1/containers/baseline` 返回每个容器的 `expected_running` / `confirmed_at` /
  `run_streak` / `suppressed`，排查误报用。

## 系统服务健康

- `v1/taskmgr/services` **只有 UGOS 应用级服务**（本机 28 项：`docker_serv`、`storage_serv`、`filemgr_serv`…），
  **没有 smbd / nfsd / sshd**。
- 系统守护进程只能从 `v1/taskmgr/processes` 的进程名判断（本机一次返回 343 条、不分页）：
  `dockerd`、`containerd`、`smbd`、`smbd-notifyd`、`sshd`、`nginx`、`avahi-daemon` 在跑；
  `nfsd`、`rpc.mountd`、`rpc.statd`、`wsdd` 不存在（NFS/WS-D 未启用）。
- 中间层关注的 11 个进程见 `HealthService.SYSTEM_WATCH`；Docker 引擎另有绝对判定
  （`v1/docker/view/GetEngineStatus` → `{"result":"online"}`）。
- 告警不按「不在清单里就报」：那样 NFS 没开就会一直误报。改为**基线型**（连续 3 轮存在后消失才报），
  需要强制常驻的进程用 `ALERT_REQUIRED_SERVICES=sshd,smbd` 显式点名（缺失即 critical）。

## 网络接口

- 两个数据源取并集：
  `v1/sysinfo/machine/common` → `hardware.net`（**只含已连通**网口，字段 `model`=网口名、`ip`、`mac`、
  `speed`、`duplex`、`mtu`、`mask`）+ `v1/taskmgr/stat/net`（含未连通网口，`series[{name,label,send_rate,recv_rate}]`）。
  本机并集结果是 `eth0`(LAN1, 1000Mbps/full/MTU1500) + `eth1`(LAN2, 未连接)。
- **错误包 / 丢弃包 / 累计流量 UGOS 不暴露**：`v1/network/*` 下除 `ipv6/nominate` 外全部 `9404`，
  `v1/sysinfo/network`、`v1/sysinfo/machine/net`、`v1/netif/list` 都是 404。
  响应里 `counters.supported=false` + `probed[]` 列出试过的路径，字段留 `null` 不编数。
- 累计流量给的是**估算值** `traffic_estimate`：用本进程记录的速率采样做梯形积分，
  `method="rate_trapezoid"`、起点是缓冲区最早采样点，不是 NAS 侧计数。
- 降速规则 `nic_speed` 保持原样（历史峰值 / `ALERT_NIC_MIN_MBPS`），与新增的 `nic_errors` 不重叠。

## 存储健康

- 池级进度都在 `v1/storage/pool/list` 的条目里，不需要额外接口：
  - `data_scrub`：`status==1` 表示正在数据校验（前端 `isOrganizeNow`）、`process` 进度、`need_time`、
    `time_remaining`、`last_time`、`next_start_time`、`is_cron`+`scheduled`。**只有 raid5/raid6 支持**，
    本机 RAID0 因此 `scrub.supported=false`。
  - `sys_sync_progress`：RAID 重建/同步进度（本机恒 0，字段语义按名称推断，未观测到真实重建）。
  - `trim`、`spare`、`is_sync_delay` 一并透出。
- 卷级：容量、`lazy_init_info`、`balance_info`、`space_alert_percent`、`warn_flags`。
- **inode 拿不到**：`volume/list`、`POST volume/describe`（要 `device`，返回 `{}`）、
  `GET volume/setting`（只有 `capacity_notify`）、`storage/overview` 都没有 inode 字段；
  `volume/describe` 用 GET 是 `9404`、用 `name` 是 `9999 no device`。
  所以 `volumes[].inode.supported=false` + `reason`，`volume_inode` 规则逻辑保留但不会触发。

---

## 告警规则（20 条）

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
| 13 | `container_down` | warning | 连续 2 轮观察到非运行（**未**建基线的容器） | 跳变 |
| 14 | `container_restart_loop` | warning | 10 分钟内重启 ≥3 次，或处于重启退避 | 跳变 |
| 15 | `nic_speed` | warning | 低于历史峰值，或低于 `ALERT_NIC_MIN_MBPS` | 跳变 |
| 16 | `container_expected_down` | warning | 连续 3 轮 running 建基线 → 连续 2 轮停止 | 基线 |
| 17 | `disk_wake_freq` | warning | 1 小时内唤醒 ≥6 次 | 跳变（滑动窗口计数） |
| 18 | `service_down` | warning / critical | 连续 3 轮存在后消失；`ALERT_REQUIRED_SERVICES` 点名缺失直接 critical | 基线 + 绝对 |
| 19 | `volume_inode` | warning | ≥90%（≥95 升 critical） | 绝对（**当前无数据源，不触发**） |
| 20 | `nic_errors` | warning | 10 分钟内错误包增量 ≥100 | 跳变（**当前无数据源，不触发**） |

阈值全部在 `.env` 里可调。`GET /api/v1/alerts/rules` 会带 `available` 与 `reason`，
第 19、20 条因 UGOS 未暴露对应字段标为 `available:false`（规则代码保留，字段出现即生效）。

**状态机语义**

- 只在 `raised`（新出现）和 `resolved`（恢复）两个时刻产生事件，持续异常不重复发。
- **绝对型规则**首轮即按真实值判定（磁盘 60°C 就是 60°C，没有「基线」可言）。
- **跳变型规则**首轮只建基线：网口峰值速率、容器运行状态、重启次数都要有上一轮样本才有意义，
  否则服务重启时会把一堆历史状态当成新故障。
- **基线型规则**（第 16、18 条）同样首轮只建基线：容器要连续 3 轮 running、服务要连续 3 轮存在，
  才标记为「期望运行 / 期望在跑」；标记后消失才告警，所以重启中间层不会把历史状态当新故障。
- **滑动窗口型规则**（第 17 条）首轮只采样不计数：休眠状态要有上一轮样本才能判「醒来」，
  否则服务启动时会把所有休眠盘当成刚被唤醒。
- `container_down` 需要连续两轮确认，避免「重启循环里恰好被采样到停止」刷屏；
  启动前就已停止的容器不会计入（只在实际发生 running→stopped 跳变时开始计数）。
- 容器三条规则互斥：命中 `container_restart_loop` 时不再发停止类告警；
  建立基线的容器只发 `container_expected_down`，不再发 `container_down`。

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
2. **UPS 字段未经真机验证** —— 本机未接 UPS，`sysinfo/machine/common` 的 `hardware.ups` 恒为 `null`，
   `/api/v1/ups` 因此固定返回 `supported:false`。已接 UPS 的分支按常见命名做多路兜底
   （`battery_charge` / `runtime` / `on_battery` 等）并标 `verified:false`，接上真实 UPS 后需校准一次。
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
11. **容器 compose 内容拿不到** —— UGOS Docker 应用的 87 个端点里没有任何 compose / project / yaml
    读取接口，`GetContainerById` 只给 `projectName`。compose 文件只以普通文件形式存在 NAS 磁盘上
    （如 `/volume1/@appstore/<app>/...`），要读只能走文件管理接口。`/containers/{id}/detail` 里
    `compose.available` 恒为 `false` 并附原因，环境变量 / 端口映射 / 卷 / 网络均可正常返回。
12. **快照在本机整体不可用** —— volume1 是 ext4，UGOS 快照只支持 Btrfs。
    接口（列表 / 创建 / 删除 / 回滚 / 计划）全部打通并实测到「NAS 明确拒绝」这一步，
    但「真的创建出一个快照」需要 Btrfs 卷才能验证。`/api/v1/snapshots` 会返回 `supported:false` + `reason`。
13. **镜像拉取没有百分比进度** —— 服务端 `progress` 恒为 0，只能靠 `status`（2=拉取中，1=完成）判断阶段；
    拉取完成后才有真实 `imageSize`。中间层不编造进度数字。
14. **历史曲线只在内存里** —— 进程重启即清空，最长 6 小时；需要长周期趋势得自己落盘或接外部 TSDB。
