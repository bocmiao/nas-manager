# UGOS Pro 实测接口契约（目标机验证通过）

> 设备：UGREEN DXP4800 Plus
> 固件：**UGOS Pro 1.19.1.0126**（Web 前端 1.19.0.78471，build 2026-08-19）
> 局域网：`https://<NAS 地址>:9443`（自签证书 CN/ORG = `UGREEN`）
> 验证时间：2026-10-25 首次；2026-10-26 复测并补齐镜像 / 快照 / 固件 / UPS / 容器详情
> ⚠️ 本文件含接口事实，不含任何凭据。

---

## 一、认证链路（实测）

### 1. 取 RSA 公钥

```
POST /ugreen/v1/verify/check
Content-Type: application/json

{"username": "<用户名>"}
```

**返回**：HTTP 200，公钥不在 body，而在**响应头 `X-Rsa-Token`**，值为 **base64 编码的 PEM**（`-----BEGIN RSA PUBLIC KEY-----`，PKCS#1 格式）。

> 坑：请求体必须带 `username`。传 `{}` 也返回 200，但 `data` 为空且**没有** `X-Rsa-Token` 头，容易误判接口不可用。

### 2. 加密密码

用上一步的公钥做 **RSA PKCS#1 v1.5**（非 OAEP）加密。前端用的是 `JSEncrypt.encryptLong`：
- RSA-2048 单块上限 245 字节
- 超长明文分段加密，每段分别 base64 后**字符串拼接**

### 3. 登录

```
POST /ugreen/v1/verify/login
Content-Type: application/json

{
  "username": "<用户名>",
  "password": "<上一步的 base64 串>",
  "keepalive": false,
  "otp": true,
  "is_simple": false
}
```

**成功返回**：`{"code":200,"msg":"success","data":{"enable_otp":false,"token":"<32 字符>"}}`
并下发 Cookie：`token`、`token_uid`（`SameSite=Strict; secure`）

- `enable_otp=true` 且无 `token` → 该账号开了二次验证，需走 OTP 流程
- 账号非 `admin` 时才会走 RSA 加密分支（前端逻辑 `"admin"!==username && password`）

### 4. 后续请求的鉴权

**主通道：`GET` + query 参数 `?token=<明文 token>`**

```
GET /ugreen/v1/taskmgr/stat/overview?token=<token>
```

实测确认：
- `GET` + `?token=` → 认证通过，返回业务数据
- `GET` 不带 token → `{"code":1010,"msg":"Token不可为空!","debug":"token format is invalid"}`
- **`POST` 带 token 会返回 `code 9404`**（版本/通道不匹配），除个别接口外不要用 POST 传 token

前端 `token_where` 有 `url` / `header` 两态，默认 `url`。切换为 header 时才用 `X-Ugreen-Token`。

### 5. 其他请求头（实测带上更稳）

```
UG-Agent: PC/WEB
Client-Version: 78471
Client-Version-Str: 1.19.0
X-Specify-Language: zh-CN
Cache-Control: no-cache
```

### 6. 加密边界（重要，能省一半工作量）

前端 `RequestEncrypt` 只在 **baseURL 不是 https** 时才包装请求体：

```js
if (!isFormContentType && notInWhitelist && !baseUrl.startsWith("https")) {
   // 才会做 encrypt_query / encrypt_req_body + req_body_sha256
}
```

→ **走 HTTPS 时请求体是明文**，只需处理 `X-Ugreen-Security-Key: md5(token)` 和可选的 `X-Ugreen-Token: RSA(token)`。
响应若含 `encrypt_resp_body` 才需要 AES-128-GCM 解密。

### 6.1 写操作的二次密码校验：`X-CurPass-Token`（实测）

磁盘测速、删存储池、删卷、热备盘创建等**高危写操作**，除 `?token=` 外还需要：

```
X-CurPass-Token: <RSA 加密后的账号密码>
```

- 值与登录时的 `password` 字段算法完全相同：`verify/check` 取 `X-Rsa-Token` → RSA PKCS#1 v1.5 加密（超长分段、base64 拼接）
- 缺失时返回 **`1101`「密码验证失败！」**
- 令牌可复用，失效后重新生成即可
- 来源：`me.post(url, body, {headers: {"X-CurPass-Token": password}})`，`password` 由前端的密码确认弹窗提供


### 7. 错误码

| code | 含义 |
|---|---|
| 200 | 成功 |
| 1000 / 1001 | 账号停用 |
| 1005 | 参数错误 |
| 1010 | Token 为空或格式非法 |
| 1024 | 会话过期，需重登 |
| 1101 | 写操作密码验证失败（缺/错 `X-CurPass-Token`） |
| 1300 | 没有权限访问（快照数据域，本机对任意 path 均如此） |
| 1301 | 不允许访问（**文件管理里最常见**：`path` 不在当前账号可见范围内，例如直接传卷根 `/volume1`；`getDirFileListV2` 参数不完整时也报这个） |
| 499 | **文件不存在**（`downloadFile` 找不到路径时；HTTP 语义的码混在业务码里） |
| 1327 | **存在同名文件夹**（`createFolder` 目标路径已存在） |
| 1302 | **路径不存在**（`getDirFileListV2` 不带 path / root_type 时） |
| 1375 | **回收站目录不存在**（该根分类支持回收站但实际没建目录，见踩坑 80） |
| 2052 | 镜像名含非法字符（`DownloadImage` 字段名不对时也报这个） |
| 5001 | 硬盘不存在（SMART/测速的 `disk` 参数值不对）；也用于**存储池不存在**（`pool/describe` 等参数名不对时） |
| 9404 | **模块内路由不存在**（storage 等模块的兜底码，见踩坑 19） |
| 9405 | 接口存在但**请求方法不对**（镜像模块只吃 POST，用 GET 即此码） |
| 32003 | 快照文件夹不存在 |
| 9999 | 服务端操作失败（含参数校验，`debug` 字段有详情） |
| 8003 | 数据库操作失败 |

---

## 二、已验证可用的只读接口

所有接口路径均需加 `ugreen/` 前缀，完整形态：`https://<ip>:9443/ugreen/v1/...`

### 系统与硬件

| 方法 | 路径 | 返回要点 |
|---|---|---|
| GET | `v1/sysinfo/machine/common` | `common`：型号/序列号/固件版本/运行时长/MAC/网口 IP；`hardware`：CPU 型号核数频率温度、内存条型号容量频率、网卡速率、UPS、USB、GPU |
| GET | `v1/verify/sn` | 设备 SN、区域、云服务域名 |
| GET | `v1/verify/is_login` | 会话状态、壁纸、头像 |
| GET | `v1/firmware/status` | 固件当前版本、升级状态、检查间隔 |
| GET | `v1/firmware/version/is_new` | `current_version` / `publish_version` / `has_version` |
| GET | `v1/user/current/user` | 当前用户 uid、昵称、账号类型、OTP 信息 |
| GET | `v1/user/config` | 用户偏好（温度单位、壁纸、组件显示） |
| GET | `v1/time/config` | 时区、时间设置、格式 |

**固件两个接口的字段实测（2026-10-26 复测）**

| 来源 | 字段 | 示例值 | 说明 |
|---|---|---|---|
| `firmware/status` | `current_version` | `1.19.1.0126` | **字符串**形态的当前版本 |
| `firmware/status` | `remote_base.versionName` | `1.20.0.0142` | 可升级到的新版本（字符串） |
| `firmware/status` | `remote_base.versionNo` | `120000142` | 同上，整数形态 |
| `firmware/status` | `remote_base.size` | `1303473152` | 新固件体积（字节） |
| `firmware/status` | `remote_base.desc` | 多行文本 | 更新说明（中文）；英文在 `firmwareRelationList` |
| `firmware/status` | `running` | `false` | 是否正在升级/检查 |
| `firmware/status` | `current_stat` / `operate_type` | `2` / `5` | **语义未确认**，不可当作"有新版本"的判据 |
| `version/is_new` | `current_version` | `119010126` | **整数**形态（同一版本号，别和上面混用） |
| `version/is_new` | `publish_version` | `120000142` | 整数形态的最新版本 |
| `version/is_new` | `has_version` | `true` | **唯一可靠的"有更新"判据** |

**UPS**：`sysinfo/machine/common` → `hardware.ups`，未接 UPS 时恒为 `null`（本机实测一直为 `null`）。
已接形态**无法在本机验证**，字段名未知。

### 实时监控（**总览页核心**）

| 方法 | 路径 | 返回要点 |
|---|---|---|
| GET | `v1/taskmgr/stat/overview` | **一屏拿全**：`cpu{used_percent,temp}` `mem{used_percent}` `net{send_rate,recv_rate}` `disk{read_rate,write_rate}` `volume{read_rate,write_rate}` `gpu` **`cpu_fan{speed,status}` `device_fan{speed,status}`** |
| GET | `v1/taskmgr/stat/get_all` | 上述全部 + 历史序列 + timestamp |
| GET | `v1/taskmgr/stat/cpu` / `mem` / `disk` / `net` / `volume` / `gpu` | 单项，含 `series` 历史序列与 `max/avg` |
| GET | `v1/taskmgr/processes` | 进程列表：`total_consume` + `list[{name,pid,process_status,status,consume{cpu_used_percent,mem_used,...}}]`，无分页 |
| GET | `v1/taskmgr/services` | 系统服务列表：`list[{name,id,appid,can_be_operated,consume{...}}]` |
| POST | `v1/taskmgr/processes/{pid}/restart` / `v1/taskmgr/services/{id}/restart|stop` | 进程/服务操作（未验证，需密码校验） |
| WSS | `v1/taskmgr/wss/subscribe` | 实时推送订阅（未验证，前端用它做秒级刷新） |

### 存储

| 方法 | 路径 | 返回要点 |
|---|---|---|
| GET | `v2/storage/disk/list` | **磁盘全清单**：型号/品牌/SN/容量/盘位/温度/通电时长/是否支持 SMART/所属存储池/是否支持定位 |
| GET | `v1/storage/pool/list` | 存储池：RAID 级别、总容量、成员盘、卷列表 |
| GET | `v1/storage/volume/list` | 卷：文件系统、总量/已用/可用、挂载点、健康状态、快照与压缩支持 |
| GET | `v1/storage/overview` | 存储总览（当前仅返回弹窗提示状态） |
| GET | `v1/storage/disk/smart/info?disk=/dev/sda` | SMART 明细，`disk` **必须传 dev_name**，返回 `report[]` 属性表 + `is_testing`/`progress`/`last` |
| GET | `v1/storage/disk/runningState?disk=/dev/sda` | 盘运行状态：`reallocated_sector_count`/`reconnect_count`/按月 `badblocks` |
| GET | `v1/storage/disk/attribute?disk=/dev/sda` | 盘身份信息：固件、转速、缓存、通电次数、标准协议 |
| POST | `v1/storage/disk/benchmark/start` | 磁盘测速开始，body `{"disk":"/dev/sda"}`，**需 `X-CurPass-Token`** |
| GET | `v1/storage/disk/benchmark/running?disk=/dev/sda` | 测速中返回 `{start_at, read_perf, write_perf}`，空闲返回 `null` |
| GET | `v1/storage/disk/benchmark/info?disk=/dev/sda` | 测速档案 + 历史 `detection_list` |
| POST | `v1/storage/disk/benchmark/stop` | 停止测速，body `{"disk":"/dev/sda"}` |
| GET | `v1/storage/disk/ihm/info?disk=/dev/sda` | 硬盘健康分析（IHM），本机 `report_code=4`（未启用） |
| GET | `v1/storage/disk/position` | 点亮盘位灯定位硬盘 |

### 快照（`com.ugreen.snapshot` 应用，entry `/snapshot`）

> 探索过程见 §四 踩坑 20~22。快照**不在**存储管理应用里，是独立应用。

| 方法 | 路径 | 参数 | 返回要点 |
|---|---|---|---|
| GET | `v1/snapshot/folder/{folder_type}` | query `page_number` / `page_size`（**必传**） | `{"folders":[],"total_number":0}`；本机 `share` 与 `home` 都为空 |
| GET | `v1/snapshot/folder/{folder_type}/settings/{folder_id}` | — | `snapshot_schedule`（frequency/interval_type/start_runtime…）+ `retention_policy`（by_day/by_number） |
| POST | `v1/snapshot/folder/{folder_type}/settings/{folder_id}` | 同上的两个对象 | 改计划；`interval_type` 不合法时报 `9999 unknown request IntervalType: 0` |
| GET | `v1/snapshot/snapshot` | query `folder_type` + `folder_id` + `page_number` + `page_size` | 某文件夹的快照列表；文件夹不存在 → `32003` |
| POST | `v1/snapshot/snapshot` | `{"folder_type":"home","folder_id":1}` | **创建快照**；非 Btrfs → `9999` + `mnt point of home is not btrfs filesystem` |
| DELETE | `v1/snapshot/snapshot` | `{"folder_type":…,"folder_id":…,"snapshots":["<名字>"]}` | **删除快照**；对不存在的快照也返回 `code 200 success`（幂等，无法据此判断） |
| POST | `v1/snapshot/snapshot/restore` | `{"folder_type":…,"folder_id":…,"snapshot":"<名字>"}` | **回滚**；同样先校验文件系统，本机报 not btrfs |
| GET | `v1/snapshot/preference/web_settings` | — | 快照文件浏览器的排序/视图偏好 |
| POST | `v1/snapshot/data/getDirFileList` | `{"limit":50,"page":1,"sort_type":1,"reverse":false,"path":"/@"}` | 浏览快照内文件；本机任意 path 均 `1300 没有权限访问` |
| GET | `v1/snapshot/data/getActionType` | — | `{"result":4}`（含义未知） |

**关键实测结论**

- `folder_type` 只接受 `share` 与 `home`；传 `volume` / `user` / `all` 返回 `9999 unknown folder type: x`。
- 缺少分页参数时错误是 `9999 操作失败!` + `debug="limit is 0, offset: 0, limit: 0"`，
  即便 query 里已经带了 `limit` / `offset` 也一样 —— 服务端读的是 `page_number` / `page_size`。
- 本机 volume1 是 **ext4**，快照全链路被文件系统挡住：
  - 列表 → `32003 文件夹不存在`
  - 创建 / 回滚 → `9999 folderServ.GetBaseFolder(): mnt point of home is not btrfs filesystem`
  - 删除 → `200 success`（空转）
- `v1/snapshot/folder/{type}/settings/{id}` 对任意 `folder_id` 都返回同一份默认值，**不做存在性校验**。
- `DELETE v1/snapshot/snapshot` **连 folder 都不校验**：`folder_type=share` + 不存在的 `folder_id`
  同样 `200 success`。因此删除结果的唯一判据是"删完再列一次"，别信返回码。
- 这些写操作**都不需要** `X-CurPass-Token`（与磁盘测速不同）。

### 日志

| 方法 | 路径 | 返回要点 |
|---|---|---|
| GET | `v1/log/query` | 分页日志：`log_id` / `level` / `module` / `operator` / `content` / `create_time` |

### Docker

| 方法 | 路径 | 返回要点 |
|---|---|---|
| POST | `v1/docker/container/ContainerListV2` | 容器清单，**必传 `{"PageNum":1,"PageSize":100}`**，返回 `{total, originalTotal, result[{name, containerId, imageName, version, projectName, status, createAt, application}]}` |
| POST | `v1/docker/container/ShowContainerList` | 同上（另一套参数） |
| GET | `v1/docker/container/StartContainer?containerId=<id>` | 启动 |
| GET | `v1/docker/container/StopContainer?containerId=<id>` | 停止 |
| POST | `v1/docker/container/BatchOperateContainer` | 批量操作 |
| POST | `v1/docker/container/ShowContainerLogs` | 容器日志，body `{"containerId": "<id>", "tail": N}`，返回 `result.message` 为 Docker 多路复用流 |
| POST | `v1/docker/container/ShowContainerDetailListV2` | **按容器的资源**：`cpuUsage` / `memUsage` / `upstreamTraffic` / `downstreamTraffic` / `restarting`，必传 `PageNum` |
| GET | `v1/docker/view/ObtainOverviewInfo` | **Docker 总览**：引擎统计 + `overviewContainers[]`（仅运行中） |
| GET | `v1/docker/view/GetEngineStatus` | `{"result": "online"}` |
| GET | `v1/docker/container/GetContainerById?containerId=<id>` | 容器详情（环境变量、网络、端口、限制），详见下方 |
| GET | `v1/docker/container/GetContainerAccessLinkInfo?port=<宿主端口>` | **容器访问规则**，参数是宿主机端口（不是 containerId）。返回 `rules[]`，见下方 |
| GET | `v1/docker/container/ShowCpu` / `ShowMemory` | **宿主机级** CPU/内存，非容器级（传 containerId 无效） |
| — | `v1/docker/container/AddTerminal` / `DelTerminal` | **容器内终端** |
| — | `v1/docker/view/GetEngineStatus` / `GetRegistryMirrors` / `SetRegistryMirrors` | Docker 引擎与镜像加速配置 |

#### 镜像（全部 POST，GET 返回 9405）

| 方法 | 路径 | 参数 / body | 返回要点 |
|---|---|---|---|
| POST | `v1/docker/image/ShowLocalImageV2` | `{"PageNum":1,"PageSize":100}`（实测可选，默认 50/页） | **本地镜像**：`originalTotal` + `result[]`，字段见下 |
| POST | `v1/docker/image/ObtainAllImages` | `{"PageNum":1,"PageSize":50}` | **Docker Hub 商店搜索**（本机 1165 条），与本地镜像无关 |
| POST | `v1/docker/image/QueryAllContaners` | `{"list":[{"imageId":"sha256:…","imageRef":"alpine:latest"}]}` | `{result:[{imageId,imageName,imageVersion,names:["容器名"]}]}`；未被引用返回 `{}` |
| POST | `v1/docker/image/DeleteImage` | `{"id":"<imageId>","status":1,"imageName":"alpine","imageVersion":"latest"}` | `{"result":"successful"}`，**不存在的镜像也返回 successful** |
| POST | `v1/docker/image/BatchDeleteImage` | `{"imageIds":[{"id":…,"status":…,"imageName":…,"imageVersion":…}]}` | 同上；`imageIds` 传字符串数组会报 `cannot unmarshal string into Go struct field` |
| POST | `v1/docker/image/DownloadImage` | `{"name":"hello-world","version":"latest"}` | `{"result":"successful"}` 立刻返回；字段名是 **name/version**，不是 imageName/imageVersion（后者报 `2052 镜像名存在特殊字符`） |

`ShowLocalImageV2` 的 `result[]` 字段：
`imageId`（`sha256:` 前缀）、`imageRef`（`name:tag`）、`imageName`、`imageVersion`（tag）、
`imageSize`（字节）、`create`（**Unix 秒**）、`status`（1=可用，2=拉取中）、`progress`（实测恒 0）、
`pullErr`（失败原因）、`imageDescribe`、`linkUrl`、`imageStatusSign`、`archType`。

**拉取过程（实测）**：`DownloadImage` 返回后，列表里先出现 `imageRef="<name>:<tag>:pull"`、`status=2`，
结束后条目变成 `name:tag`、`status=1` 并带上真实 `imageSize`。没有字节级进度可用。

#### 容器详情 `GetContainerById`

| 字段 | 说明 |
|---|---|
| `containerName` / `imageName` / `imageId` / `tag` / `imageVersion` | 身份 |
| `projectName` | 所属应用（compose 项目名），**没有 compose 内容** |
| `environmentVariables[]` | `{variable, price}` —— **`price` 就是 value**（UGOS 字段名拼写错误） |
| `portMapping[]` | `{nasPort, containerPort, portType}` |
| `volumes[]` | `{nasDirectory, containerDirectory, containerPermission}`（可能为 `null`） |
| `networkMode` / `subnetSettings[]` / `nasIPs` | 网络 |
| `cpuLimit` / `memLimit` / `noRestrictions` / `privilegedMode` | 资源与权限 |
| `containerRunCommand` | 运行命令（本机多为 `null`） |

`ShowContainerDetailListV2` 的 `volumes` / `portMapping` 与上面同构，但 `nasPort` / `containerPort` 的
**类型不稳定**——同一台设备上，`safewoo-v2ray-mono` 给 `"666"`（字符串），`halo` 给 `8090`（数字）。
解码时必须两种都吃下，否则整个容器列表会因为一个容器解析失败而全灭（踩坑 72）。

`ContainerListV2` **不返回 `portMapping`**，端口只在 `ShowContainerDetailListV2` 和 `GetContainerById` 里有。
好消息是前者一次就能取全部容器，不用逐个调详情。

#### 在应用内打开容器 Web UI（2026-10-26 实测）

需求来源：装到 Docker 里的面板（青龙、Halo、qBittorrent…）想在 App 里直接打开操作。

结论：**可行，路径是「宿主端口直连」**，官方自己也是这么做的。

`GetContainerAccessLinkInfo?port=<宿主端口>` 返回的是**访问规则配置**，不是现成 URL：

```json
{
  "rules": [
    {"linkType": 1, "accessType": 4, "dockerAccess": {"port": "5777"},
     "isAuthRequired": false, "disableRemoteAccess": null},
    {"linkType": 2, "accessType": 1,
     "portAppAccess": {"port": "5777", "httpsEnable": false,
                       "httpsPort": "", "urlPath": "", "linkType": 2}}
  ],
  "openType": "",
  "supports": ["pc", "app"]
}
```

能落成地址的只有 `portAppAccess` 里的四个字段：

| 字段 | 用途 |
|---|---|
| `port` | 对外端口 |
| `httpsEnable` / `httpsPort` | 是否是 https、https 用哪个端口（可能和 http 不同） |
| `urlPath` | **子路径**，有些应用的 UI 不在根路径，必须拼上 |

`linkType` / `accessType` 的取值语义（1/2/3/4、1/4/5）**尚未从 bundle 完全确认**，
因此中间层原样透出，不做猜测性翻译。`disableRemoteAccess` 非空表示该链路禁用了远程访问。

可达性实测（同一台设备，16 个容器）：

| 容器类型 | 宿主端口 | 状态 | `http://<nas>:<port>/` 实测 |
|---|---|---|---|
| 自建面板类应用 | 13888 | running | **200 + `text/html`，7255 字节真实页面** |
| 任务调度面板 | 5777 | running | 连不上（容器在跑但端口未响应，可能应用自身没起 Web） |
| 游戏服务端 | 25565 | running | 连不上——**Minecraft 是纯 TCP 协议，不是 HTTP** |
| 博客 CMS | 8090 | exited | Connection refused |
| 业务应用 | 3100 | exited | Connection refused |
| 代理服务端 | 666 | exited | Connection refused |

**这条实测最有价值的结论：端口通 ≠ 能当网页打开。** 16 个容器里 10 个根本没映射端口，
映射了端口的 6 个里只有 1 个是真 HTTP 服务。所以打开前必须先探测，否则用户点进去只会白屏。

服务端只负责拼地址、不负责探测（服务端探测等于开了个 SSRF 口子，而且慢）；
探测放在客户端：先 `http://host:port/`，拿不到 HTTP 响应再试 `https://host:port/`，
两者都失败时按错误类型区分「端口不通」和「端口通但不是 HTTP」。

### 文件管理（2026-10-26 全链路真机验证）

`filemgr` 的接口面很大（前端 bundle 里能抓到 60+ 个 `v1/filemgr/*` 和 30+ 个 `v2/filemgr/*`），
下面只列真正跑通的。探针脚本：`scripts/probe_files.py`、`scripts/probe_files2.py`。

#### 根分类

| 方法 | 路径 | 参数 | 返回 |
|---|---|---|---|
| GET | `v2/filemgr/getRootDirs` | `client=pc` | `{root_list: [2, 3, 4, 6, 9]}` —— **纯数字数组，没有对象** |
| GET | `v1/filemgr/getVolumes` | — | `{result: [{name, path, fs_type, free, used, all, describe}]}` |
| GET | `v1/filemgr/getSharedFolderList` | — | `{total, data: []}`（非管理员账号为空） |

`root_list` 的数字与分类的对应来自前端 `initCache()` 的静态表：

| root_type | 分类 | 本机 |
|---|---|---|
| 1 | 收藏 | ✗ |
| 2 | 个人文件夹 | ✓（`top_path=/home/<user>`） |
| 3 | 共享文件夹 | ✓ |
| 4 | 用户文件夹 | ✓ |
| 5 | 远程挂载 | ✗ |
| 6 | 外部设备 | ✓ |
| 7 | 镜像（ISO） | ✗ |
| 8 | 共享给我 | ✗ |
| 9 | 标签 | ✓ |
| 999999 | 搜索 | —（仅前端虚拟节点） |

#### 目录列表

`POST v2/filemgr/getDirFileListV2`

| 字段 | 说明 |
|---|---|
| `root_type` | 根级用，整数 |
| `path` | 子路径用，绝对路径字符串。**与 `root_type` 互斥** |
| `permission` | 共享根（`root_type=3`）必须传 `4`，否则列不出来 |
| `data_type` | `2`=只要右侧列表 / `1`=只要左树 / `0`=都要 |
| `limit` / `page` | 分页，`page` 从 1 开始 |
| `sort_type` | 1 名称 / 2 大小 / 3 类型 / 4 修改 / 5 创建 / 6 访问 / 7 删除时间 |
| `reverse` | 升降序 |
| `is_shield_recycle` / `left_no_page_show` / `left_count` / `intranet_share_id` / `version` / `operate` | 固定值：`false` / `false` / `5000` / `0` / `1` / `0` |

返回：
```json
{"left_tree": {...}, "right_files": {"total": 11, "top_path": "/volume1/docker",
  "permission_mask": 7, "files": [ {...条目...} ]}}
```

- `top_path` 是**这棵树的根**，不是当前目录（列 `/home/<user>/Music` 时它仍返回 `/home/<user>`）
- 条目 `file_type`：**1 = 目录，0 = 文件**；条目约 30 个键，多数是前端内部状态
- `root_type=9`（标签）返回的是**标签定义**（`tag_id` / `name` / `colour`），没有 `file_type`，
  不应当成文件处理

#### 下载（三步，缺一不可）

| # | 方法 | 路径 | 参数 | 返回 |
|---|---|---|---|---|
| 1 | POST | `v2/filemgr/addPathsTask` | `{paths: [{path, intranet_share_id}]}` | `{result: "<task_uuid>"}` |
| 2 | GET | `v2/filemgr/getDownloadToken` | `task_uuid` + `coding=true` + `is_livephoto=false` | `{dl_token}`（实测 32 字符） |
| 3 | GET | `/ugreen/v2/filemgr/downloadFile` | `token=<dl_token>` | **原始字节**（带正确 `content-type`） |

第 3 步用的是 `dl_token`，**不是会话 token**——前端同样关掉了 `tokenAuth`。
服务端按 `chunked` 返回，**没有 `Content-Length`**。

#### 写操作

| 方法 | 路径 | 载荷 | 要点 |
|---|---|---|---|
| POST | `v2/filemgr/createFolder` | `{path: "<新目录完整路径>"}` | `path` 是**目标目录本身**不是父目录；已存在 → `1327` |
| POST | `v2/filemgr/rename` | `{path, new_name}` | 返回更新后的条目（`path` 已变） |
| POST | `v2/filemgr/delPaths` | `{paths: ["<路径字符串>"], forever: bool}` | **只认字符串数组**，传对象数组 → `9999 操作失败!` |
| POST | `v1/filemgr/getPathFreeSize` | `{path}` | `{result: <bytes>}` |
| GET | `v1/filemgr/determinePath` | `path` | 权限预检：`{result: null, code: 200}` 表示通过 |
| GET | `v1/filemgr/queryReadable` | `path` | 可读性预检，`code == 200` 即可读 |

`forever=false` 走回收站（与网页端默认一致），`true` 直删。

#### 上传（参数全在 header，body 是裸字节）

`POST v1/filemgr/fileUploadV2`（注意是 **v1**，不是 v2）

| header | 值 |
|---|---|
| `Content-Type` | `application/octet-stream` |
| `Content-Disposition` | `attachment; filename="<文件名 URL 编码>"` |
| `ug-param` | 单行 JSON，见下 |

```json
{"uuid":"<文件名 md5 hex>","file_name":"<URL 编码>","action_type":3,
 "size":<总字节>,"current_size":<本块字节>,"resume":true,
 "dir":"<目标目录 URL 编码>","change_time":<int64 unix 秒>,
 "is_live_photo":false,"first_request":false,"begin_size":<本块偏移>}
```

返回 `{result: "<最终落盘路径>"}`——注意 `result` 可能是改名后的路径。

**分块是追加语义**：实测 400B（`begin_size=0`）+ 400B（`begin_size=400`）
→ 800B 文件，内容正确拼接。`size` 每块都传总长，`current_size` 是**本块**长度。

`action_type` 实测行为：

| 值 | 同名文件 |
|---|---|
| 0 / 1 / 2 | **静默覆盖**（原文件没了，没有任何提示） |
| 3 | 自动改名成 `名字(1).后缀` |

管理端默认用 `3`——覆盖是破坏性操作，不能让用户误触。

中文文件名与中文目录都可以传，前提是 `file_name` / `dir` 都做了 URL 编码
（header 的值只能是 ASCII，不编码会直接编码失败）。

#### 已知但未实现的 filemgr 能力

前端 bundle 里还有这些接口，未做实测（按需再探）。
上传端点已经跑通（见上面「上传」小节），但**续传**（`v1/filemgr/fileUpload` 预检 +
`getUpdateTmpInfo` 查询已传大小 + `delUpTemFile` 清理临时文件）只验证到"预检返回
`1005 request message format error`"，没有继续逆 multipart 分支——单块/多块从头传已够用。
`getAllLevelsPath`（整树递归）、`createsearchtask` / `getsearchtaskresult`（异步搜索）、
`cpOrMvPath`（复制/移动，`{dst, src, action_type, type, intranet_share_id}`）、
`thumbnail` / `getIcon`（缩略图）、`compressedFile` / `unCompressedFile`（压缩解压）、
`mountIso` / `umountIso`、`mvToVault`（保险箱）、`emptyRecycles`（清空回收站）。

### 用户与用户组（控制面板 `/ctlmgr`，2026-10-26 实测）

> 没有独立的「用户管理」应用，端点全在 `/ctlmgr` 入口 bundle 里。
> `api-version: 2` 请求头对下面这些接口**没有影响**（带不带结果一致），前端才需要它。

| 方法 | 路径 | 参数 | 返回要点 |
|---|---|---|---|
| GET | `v1/user/list` | `reverse=false` | `{"list":[{uid,username,role,groups[{gid,name}],account_type,is_domain,is_expired,expire_time,enable_home_dir,dir_quota,otp_info{...}}]}` |
| GET | `v1/user/uid/{uid}` | — | 单用户，字段同上（**没有**登录时间） |
| GET | `v1/user/permission/{uid}` | — | `{"result":[{id,name,path,type,permission,permission_mask,time_backup}]}`；`permission` 实测为 `-1`，真实掩码看 `permission_mask`（7） |
| GET | `v1/user/authorized/{uid}` | — | `{"result":[{id:"com.ugreen.pro.usermgr", allow:false}]}` 应用级授权 |
| GET | `v1/user/quota/{uid}` | — | `{home:{path,used,soft,hard,file_system,max_value}, ext4:[{path,label,user,used,sub:[...]}], btrfs:[]}` |
| GET | `v2/user/directory/quota` | — | 当前用户的家目录配额（`home_dir` / `max_value` / `has_user_quota`） |
| GET | `v1/user/config/global` | — | 全局密码策略（`min_length` / `must_contain_case` / `disable_common_pwd`…） |
| GET | `v1/user/config/password` | — | 密码有效期策略（`max_days` / `warn_age` / `force_change` / 白名单） |
| GET | `v1/user/passwd/lastChangeTime/{uid}` | — | `{"result":<Unix 秒>}` 最后一次改密时间 |
| GET | `v1/user/invite/state/{uid}` | — | `{"result":"<邀请码>"}` |
| GET | `v1/user/headpic/list` | — | 可选头像文件名数组 |
| GET | `v1/user/priority_link` | — | DDNS / uglink / 端口信息（含内网 IP，注意脱敏） |
| GET | `v1/group/list` | `reverse=false` | `{"list":[]}` —— **只列自定义组**，内置组不出现 |
| GET | `v1/group/permission/{gid}` | — | 组共享权限 `{"result":[...]}`；`permission_mask` 可为 `-1` |
| GET | `v1/group/authorized/{gid}` | — | `{"result":null}`（本机） |
| POST | `v2/user/permission` | `{"uids":[1003]}` 或 `{"uid":1003}` | `{data:[{email,icon_type,icon_value,name,id,folder_list[{id,name,path,type,permission,permission_mask}]}],total,cur_page}` |
| POST | `v2/group/permission` | `{"gids":[10]}` | `{data:[],total:0,cur_page:0}`（内置组无自定义权限记录） |
| GET | `v1/filemgr/getShareList` | — | 共享文件夹：`id/type/name/path/permission_mask/filesystem/owner/is_mount/ctime/mtime` |
| GET | `v1/user/check/quota` | — | `{"result":false}` 是否启用配额 |

**实测结论**

- `role` 取值实测为 `"admin"`；判断管理员同时看 `role=="admin"` 与 `groups` 里是否有 `admin`。
- 本机内置组：`admin`(10)、`users`(100)、`docker`(121)、`ughomeusers`(133)，只出现在用户的 `groups` 里。
- `v1/group/info/10` → `9999 record not found`；但 `v1/group/permission/10` 正常返回 3 个共享。
- `v1/group/gids/permission` 传 `{"gids":[10]}` → `9999 group: unknown group`。
- 写操作（`user/create|edit|delete|permission`、`group/*`、`otp/disable`）**均未验证、未实现**。

### 硬盘休眠 / 电源

| 方法 | 路径 | 返回要点 |
|---|---|---|
| GET | `v1/hardware/power/config` | **休眠策略**：`hard_drive_flag`（启用）、`hard_drive_unit`(M/H)、`hard_drive_time`(20)、`hard_drive_strategy`、`hard_drive_target_info{pools[{pool_name,disk_in_pool[{dev_name,is_selected,support_idle,not_support_reason}]}],free_disks[]}`、`usb_hard_drive_*`、`enable_hard_drive_sleep_log`；同接口还返回开机计划 `time_switches` / `power_boot` / `wake_on` |
| GET | `v2/storage/disk/list` | 每块盘的 **`is_standby`**（布尔）就是休眠状态；另有 `is_frozen` / `is_support_ihm` / `dev_name` |
| GET | `v1/storage/disk/runningState?disk=/dev/sda` | `status`（实测 1=工作中）、温度、`power_start_time`、`reconnect_count`、`rescan_count`、`spin_retry_count` |
| GET | `v1/storage/disk/vars` | `{result:[{name:"sda",temperature:36},…]}` 轻量温度 |
| GET | `v1/storage/disk/cron/list` | 磁盘检测计划（SMART/IHM），含 `next_time` 与 `disk_list` |
| GET | `v1/storage/disk/setting/get` | `{"badblocks":1}` 仅此一项 |

**不存在的路径**（全部 `9404`）：`v1/storage/disk/sleep`、`/standby`、`/hibernate`、`/power`、
`/power/state`、`/status`。`v1/power/disk/list`、`v1/power/config` 是 **HTTP 404 非 JSON**。
→ 没有「按需查某块盘当前是否休眠」的接口，只能读磁盘列表的 `is_standby`。

**休眠实测**（2026-10-26 08:18–08:43，13 轮 × 120s）：两块盘 `is_standby` 恒 `false`，
温度稳定 36/38°C。休眠策略为「空闲 20 分钟」，观察窗口已越过该阈值仍未休眠 —— 机器上常驻 9 个容器
（qBittorrent / Lucky / 青龙 / Minecraft 等）持续产生磁盘 IO，空闲计时不断被打断。
→ `is_standby=true` 的真实报文本次**未采到**，字段含义按前端逻辑与布尔语义判定；
「睡→醒」跳变与频繁唤醒告警用合成数据验证。

### 存储健康（池 / 卷）

| 方法 | 路径 | 返回要点 |
|---|---|---|
| GET | `v1/storage/pool/list` | 每个池都带 `data_scrub`（校验）、`trim`、`spare`、`sys_sync_progress`、`is_sync_delay`、`sync_time`、成员盘 `disks[]`、`volumes[]` |
| GET | `v1/storage/volume/list` | 卷：容量、`health`、`warn_flags`、`space_alert_percent`、`lazy_init_info`、`balance_info`、`dirty_block_flush_status`、`support_*` 能力位 |
| POST | `v1/storage/volume/describe` | 必须传 `{"device":"/dev/mapper/ug_xxx-pool1-volume1"}`，本机返回 `{}`；传 `{"name":...}` 报 `9999 no device`；用 GET 报 `9404` |
| GET | `v1/storage/volume/setting?device=...` | `{"description":"","capacity_notify":{"percent":0}}`；不带 device 报 `9999 device empty` |
| GET | `v1/storage/pool/setting/get` | `{"raid_speed_limit":{"mode":0},"bitmap":{"enable":1}}` |
| GET | `v1/storage/overview` | `{"popup_info":{"newer_popup":true}}` |
| GET | `v1/storage/overview/widget` | `{"result":[{"type":1,"flags":[1,2]}]}` |

`data_scrub` 实测字段：`pool_name/pool_label/is_cron/need_time/time_remaining/process/status/last_time/next_start_time/whether_to_clean_up/scheduled{interval,interval_unit,first_run_datetime}`。
前端逻辑 `(data_scrub.status)==1 → isOrganizeNow`，即 **status=1 = 正在校验**，且只对 `raid5/raid6` 有意义。

**inode：不存在。** `volume/list`、`volume/describe`(POST)、`volume/setting`、`storage/overview` 都没有 inode 字段。

### 任务管理器 / 进程

| 方法 | 路径 | 返回要点 |
|---|---|---|
| GET | `v1/taskmgr/services` | **只有 UGOS 应用级服务**（本机 28 项：`docker_serv`/`storage_serv`/`filemgr_serv`/`ctl_serv`…），字段 `{name,id,appid,icon_path,icon_mtime,can_be_operated,consume{...}}`；**没有** smbd/nfsd/sshd，也**没有**运行状态位（只列在跑的） |
| GET | `v1/taskmgr/processes` | 全量进程（本机 343 条、不分页）：`{name,pid,process_status,status,consume{...}}`；`smbd`/`smbd-notifyd`/`dockerd`/`sshd`/`nginx`/`avahi-daemon` 都在这里 |
| GET | `v1/docker/view/GetEngineStatus` | `{"result":"online"}` |

### 网络

| 方法 | 路径 | 返回要点 |
|---|---|---|
| GET | `v1/sysinfo/machine/common` | `hardware.net[]`：`{model:<网口名>, ip, mac, speed(Mbps), duplex, mtu, mask}`，**只含已连通网口**（本机只有 eth0，缺 eth1） |
| GET | `v1/taskmgr/stat/net` | `series[{time,name,label,send_rate,recv_rate}]`，`name` 含未连通网口（本机 eth0+eth1），`label`=LAN1/LAN2；**只有瞬时速率** |

**不存在的路径**：`v1/network/interface/list`、`v1/network/list`、`v1/network/info`、`v1/network/status`、
`v1/network/nic/list`、`v1/network/ethernet/list`、`v1/network/stat`、`v1/network/traffic`、
`v1/network/lan/list`、`v1/network/bond/list`、`v1/network/config`、`v1/network/statistic` 全部 `9404`；
`v1/sysinfo/network`、`v1/sysinfo/net`、`v1/sysinfo/machine/net`、`v1/net/list`、`v1/netif/list` 全部 **404**。
→ **错误包 / 丢弃包 / 累计流量没有任何接口**。`v1/network/` 模块只确认存在 `ipv6/nominate`。

---

## 三、应用清单与 SPA 入口

`GET /ugreen/v1/desktop/module/list` 返回所有已安装应用（`module_id` / `name` / `entry` / `version` / `app_status`）。

逆向某应用接口的方法：
```
GET https://<ip>:9443<entry>/            → HTML，提取 src="*.js"
GET https://<ip>:9443<entry>/js/*.js     → 正则提取 "v\d+/模块/动作"
```

本机已装应用（节选）：Docker、文件管理、存储管理、任务管理器、控制面板、日志中心、快照、下载中心、迅雷、同步与备份、影视中心、音乐、相册、在线文档、保险箱、文件版本管理、网盘工具、安全管家、应用中心、全局搜索、模型管理，以及 Docker 衍生应用 Lucky / 青龙 / qBittorrent / AI Plugins / 节点小宝。**未安装 KVM 虚拟机应用**。

各应用 `entry` 对照（`GET v1/desktop/module/list` 的 `entry` 字段，实测）：`/docker`、`/filemgr`、`/storagemgr`、
`/taskmgr`、`/ctlmgr`、`/logmgr`、**`/snapshot`（快照）**、`/version`（文件版本管理）、`/syncbackup`、`/appmgr`。

> 注意：**存储管理 `/storagemgr` 的 bundle 里没有任何快照接口**（19 个懒加载分块全量搜索 `snapshot` 只命中
> `snapshot_size` 这类空间占用字段）。快照功能在独立的 `/snapshot` 应用里，接口前缀是 `v1/snapshot/*`。

---

## 四、踩坑记录

1. **`/ugreen/` 前缀不能漏**。前端拦截器统一加 `PREFIX = "ugreen/"`，直接请求 `/v1/...` 会得到空 body 的 404，很容易误判为接口不存在。
2. **`verify/check` 必须带 username**，且公钥在响应头 `X-Rsa-Token`（base64），不在 body。
3. **POST 带 token 多数接口返回 9404**，读操作优先用 GET + `?token=`。
4. **容器列表必须传 `PageNum`**，否则返回 HTTP 400 + `Field validation for 'PageNum' failed on the 'required' tag`。
5. **不同应用版本路径不同**：磁盘列表在 `v2/storage/disk/list`，文件列表 v1/v2 并存，需按 `code` 做降级重试。
6. **`v1/idx/info` 在这台机器上返回 8003**（数据库操作失败），与认证无关，属接口自身问题，不要当认证失败处理。
7. **Git Bash 会把 `/taskmgr` 转成 Windows 路径**，抓 SPA 时要设 `MSYS_NO_PATHCONV=1`。
8. 系统版本要看 `sysinfo/machine/common` 的 `system_version`（1.19.1.0126），桌面 HTML 的 `data-version` 是 Web 前端版本（1.19.0.78471），两者不一致。
9. **磁盘 `status` 的 `0` 和 `1` 都是正常态**（`1` = 已激活在用）。按 `!= 0` 判异常会误报所有在用磁盘。
10. **`stat/overview` 的 `volume.read_rate` / `write_rate` 量级不稳定**：实测同一台机器先返回 `4.2e8`（≈402 MB/s），后又返回 `1.09e12`（≈1 TB/s）。怀疑是采样窗口内的累计字节数而非瞬时速率，需要对照前端图表逻辑才能定论。App 端不要直接当 B/s 渲染。
11. **`POST` + `?token=` 是有效的**（`ContainerListV2` 验证通过），前提是 body 参数合法。之前把 POST 一律判为 9404 是错的——9404 只在缺参数或走 header 传 token 时出现。
12. **容器操作是 GET**：`StartContainer` / `StopContainer` / `RestartContainer` 全部用 `GET ?containerId=<id>`，删除容器才是 POST `RemoveContainer`。
13. **`ShowContainerLogs` 是 POST**，请求体 `containerId` + `tail`（首字母大小写均可）。**服务端会忽略行数参数**，永远返回完整日志，`tail` 只能在客户端截断。返回的 `result.message` 是 Docker 多路复用流（8 字节帧头 = 1B 流类型 + 3B 保留 + 4B 大端长度），且**长度按 UTF-8 字节计而响应已是字符串**，解帧要按字符累加其 UTF-8 字节数对齐。
14. **`storage/disk/smart/info` 的 `disk` 参数必须传 `dev_name`**（形如 `/dev/sda`，取自 `v2/storage/disk/list` 的 `dev_name` 字段）。
    - 不传 → `9999`
    - 传裸盘名 `sda` / 标签 `硬盘1` / 盘位 `ata1` / 序列号 → `5001 硬盘不存在`
    - 这条同样适用于 `benchmark/*`、`runningState`、`attribute`、`ihm/info`
15. **磁盘测速是「发起 + 轮询」两步**：`POST benchmark/start` 后由前端每 2 秒 `GET benchmark/running`，
    返回 `null` 即表示结束，结果去 `benchmark/info` 的 `detection_list` 取（`read_perf.bw` / `write_perf.bw`，单位 MB/s）。
16. **在用磁盘不能测速**：`benchmark/start` 对存储池内的盘返回 `9999` + `debug="disk is busy: 1"`。
    这是业务规则，不是权限或参数问题，需要一块**未加入存储池**的盘才能跑通。
17. **`ShowCpu` / `ShowMemory` 是宿主机级接口**，不是容器级。按容器的 CPU/内存/流量要用
    `ShowContainerDetailListV2`（`cpuUsage`/`memUsage`/`upstreamTraffic`/`downstreamTraffic`/`restarting`）
    或 `view/ObtainOverviewInfo`（只含运行中容器）。`memoryUsagePercentage` 字段名有误导，实际单位是**字节**。
18. **写操作要 `X-CurPass-Token`**：值为 RSA 加密后的密码，缺失返回 `1101`。见 §6.1。
19. **`9404` 是"模块内路由不存在"的兜底码**，不是只有"客户端版本不匹配"。
    实测 `GET v1/storage/foo/bar`、`GET v1/storage/xxx` 与 `GET v1/storage/snapshot/list` 全部返回 `9404`，
    而同一模块的 `v1/storage/volume/list` 正常 200。→ 用 9404 判断"接口存在但版本不对"会误判。
    真正的"路径不存在"是 **HTTP 404 + 非 JSON 响应**。
20. **镜像接口只吃 POST**：`GET v1/docker/image/ShowLocalImageV2` 返回 `9405`，
    同一路径 POST 正常。判断接口是否存在时，必须 GET/POST 都试一次。
21. **`ObtainAllImages` 不是本地镜像列表**，而是 Docker Hub **商店搜索**（本机 1165 条、带 logo/分类/架构）。
    本地镜像要用 `ShowLocalImageV2`（本机 38 条）。这对命名有强烈误导性。
22. **快照端点在独立应用 `/snapshot` 里**：存储管理 bundle 全量搜索无 `snapshot` 接口；
    而 `/snapshot` 应用入口 JS 本身也搜不到，端点藏在它的 3 个懒加载分块
    （`index-B1-*.js` 有 `folder/*`，`index-Bgl3*.js` 有 `data/*`）里。
    最终靠"路由不存在=404 / 存在但参数错=9999"这一差异暴力枚举出
    `v1/snapshot/snapshot`（POST 创建 / GET 列表）与 `v1/snapshot/snapshot/restore`（回滚）。
23. **快照列表的分页参数名是 `page_number` / `page_size`**，不是 `limit` / `offset`、也不是 `page` / `size`。
    传错时错误信息里出现的 "limit is 0" 会误导你以为要传 `limit`。
24. **快照只支持 Btrfs**：ext4 卷上创建/回滚直接 `9999 ... is not btrfs filesystem`，列表 `32003 文件夹不存在`。
    同一台机器上"接口通了"和"功能可用"是两件事。
25. **删除类接口的幂等陷阱**：`DeleteImage`、`BatchDeleteImage`、`DELETE v1/snapshot/snapshot`
    对**不存在**的目标都返回 `success/successful`；镜像被停止的容器引用时 Docker 拒绝删除，
    UGOS 仍然回 `successful`。判断真伪只能"删完再列一次"——中间层两个模块都按这个做，
    并返回 `verified` / `still_present`（`snapshot.delete` / `images.delete`）。
26. **`BatchDeleteImage` 的 `imageIds` 必须是对象数组**，元素形如
    `{id,status,imageName,imageVersion}`；传 `["sha256:…"]` 会报
    `json: cannot unmarshal string into Go struct field BatchDeleteImageRequest.imageIds`。
27. **固件版本号有字符串和整数两套**：`firmware/status.current_version="1.19.1.0126"` 对
    `firmware/version/is_new.current_version=119010126`。只有 `is_new.has_version` 能可靠判断"有更新"。
28. **容器详情里的环境变量字段叫 `price`**（`{variable, price}`），不是 `value` ——
    UGOS 前端把 `value` 拼错成了 `price`，实测确实如此。
29. **`GetContainerById` 没有 compose 内容**。docker 应用 87 个端点里没有任何 compose/project/yaml 读取接口，
    只有 `projectName` 字段。compose 文件以普通文件存在 NAS 磁盘上。

---

## 四之二、第三批踩坑（用户 / 休眠 / 服务 / 网络 / 存储健康）

30. **用户接口没有登录时间**。`v1/user/list`、`v1/user/uid/{uid}` 都没有 last_login 类字段；
    `v1/user/login/record`、`v1/user/last/online`、`v1/user/online/list`、`v1/user/session/list`
    等猜测路径全是 `9404`。唯一可行的是**反查审计日志**：`v1/log/query?keyword=<用户名>`。
    Web/App 登录形态 `module=login` / `operator=<用户名>` / `content="User [x] from [ip] logged in successfully."`；
    SSH 登录形态 `module=control_panel` / `operator=system` / `content="User <用户名> logged into the system via SSH."`
    —— 这时用户名只在正文里，只按 operator 匹配会漏。
31. **`v1/log/query` 的列表字段叫 `log_list`**，不是 `list`/`result`/`data`。按后三种键解析会得到空列表，
    表现为「日志明明有记录但判断成没有」。
32. **`v1/group/list` 只列自定义组**：本机返回 `{"list":[]}`，而内置 `admin`(10)/`users`(100)/`docker`(121)/
    `ughomeusers`(133) 只出现在用户的 `groups` 字段里。`v1/group/info/10` 报 `9999 record not found`，
    但 `v1/group/permission/10` 正常返回共享权限。→ 组列表要「自定义组 ∪ 用户 groups 归纳」。
33. **权限掩码有两个字段且不一致**：`v1/user/permission/{uid}` 里 `permission=-1`、`permission_mask=7`；
    `v2/user/permission` 里 `permission=7`。组权限里还出现过 `permission_mask=-1`。
    → 只信 `permission_mask`，且负值不要按位解析。
34. **`otp_info` 里的 `secret` 是敏感字段**，用户列表接口会原样返回。中间层必须过滤，
    只暴露是否启用（`status` 0=未启用，实测本机无需 OTP 即可登录）。
35. **硬盘休眠没有专用查询接口**：`v1/storage/disk/{sleep,standby,hibernate,power,status}` 全部 `9404`，
    `v1/power/*` 是 404。唯一信号是 `v2/storage/disk/list` 的 **`is_standby`**（布尔）。
    休眠策略在 `v1/hardware/power/config`（`hard_drive_flag`/`unit`/`time`/`target_info`）。
    → 只能给 active / sleeping 两态；ATA 层的 standby 与 sleep 无法区分，别编第三个值。
36. **读 SMART 会把休眠盘唤醒**。中间层巡检原本每轮读全部盘的 SMART，等于自己破坏用户的休眠策略。
    现在对 `is_standby=true` 的盘跳过 SMART，代价是休眠期间 SMART 数据停在旧值。
    → 告警侧必须把「跳过」当成「本轮无数据」（既不新增也不恢复），否则每次休眠/唤醒
    都会刷一对「已恢复 / 又异常」的假通知。`all_smart()` 因此返回 `(明细, 跳过的盘名)`。
    → 任何「轮询式」监控接 UGOS 都要注意这条：读盘就会让盘转起来。
37. **`v1/taskmgr/services` 不是 systemd 服务列表**，只是 UGOS 应用清单（本机 28 项），
    没有 smbd / nfsd / sshd，也没有运行状态字段（只列在跑的）。
    系统守护进程只能从 `v1/taskmgr/processes`（本机 343 条、不分页）的 `name` 里找。
38. **`hardware.net` 只含已连通网口**：本机是双 2.5G 机型、两个网口，但 `hardware.net` 只有 `eth0`；
    `eth1` 只在 `v1/taskmgr/stat/net` 的 series 里出现（`send_rate/recv_rate` 恒 0，`label=LAN2`）。
    要列全部网口得把两个来源取并集，未连通口的 `connected` 只能给 `null`（不知道是没插线还是禁用）。
39. **网口错误包 / 丢弃包 / 累计流量完全拿不到**：`v1/network/` 模块除 `ipv6/nominate` 外全 `9404`，
    `v1/sysinfo/network` 一类是 404。只在接口里明确 `counters.supported=false`，
    累计流量只能用中间层自己的速率采样积分估算，并标 `method=rate_trapezoid`。
40. **inode 不存在**：`volume/list` 无 inode 字段；`volume/describe` 是 **POST + `device` 参数**
    （GET 报 `9404`、传 `name` 报 `9999 no device`、正确传 device 返回 `{}`）；
    `volume/setting` 只有 `capacity_notify`；`storage/overview` 只有弹窗标记。
    → inode 告警只能保留逻辑、标不支持，不能自造数据。
41. **`storage/pool/{describe,capacity,extendInfo}` 一直 `5001 存储池不存在`**：
    试过 `pool` / `name` / `poolname` / `id` / `pool_name` 五种参数名，都会报 5001。
    池级健康数据（校验、同步、trim、spare）其实**都在 `pool/list` 的条目里**，不必再调这些接口。
42. **`data_scrub.status == 1` 才是「校验中」**，来自前端 `isOrganizeNow` 逻辑；
    `process` 是百分比，`need_time`/`time_remaining` 单位未确认。且只有 raid5/raid6 会跑校验，
    RAID0/RAID1 上这些字段恒 0/`"--"`。
43. **`v1/taskmgr/stat/net` 和 `stat/disk` 的 `series` 里混着一条 `name="overview"` 的汇总项**，
    按 name 建索引时记得排除，否则会多出一个假网口/假磁盘。


---

## 五、验证环境参考值（2026-10-25 快照，已脱敏）

> 以下为开发验证时的典型值，用于对照字段含义与量级。主机名、IP、序列号等可识别标识已隐去。

| 项 | 值 |
|---|---|
| 主机名 | DXP4800PLUS-XXXX |
| 固件 | 1.19.1.0126 |
| 已运行 | 数十天量级 |
| CPU | Intel Pentium Gold 8505，5 核 6 线程 |
| 内存 | 16GB DDR5-5600，占用约三分之一 |
| 网口 | eth0 192.0.2.10，**协商 1000Mbps 全双工**（机型支持 2.5G） |
| 风扇 | 机箱数百 RPM，CPU 风扇停转 |
| 存储池 | pool1，**RAID0**，2 盘，合计约 7.2 TB |
| 卷 | volume1，ext4，已用约 45%，挂载 `/volume1`、`/home`、`/opt/<third-party-panel>/data` |
| 磁盘 | 2 × 4TB 机械盘，三十余度，通电 6000+ 小时，支持 SMART |
| 容器 | 十余个，多数运行中 |

**风险提示**
- **RAID0 + 2 盘 = 零冗余**，任一盘损坏全部数据丢失。
- 网口只协商到 **1000Mbps**，机型是双 2.5GbE，怀疑上游是千兆交换机或网线为 Cat5e。
- `/opt/<third-party-panel>/data` 显示已装第三方面板，与 UGOS 自带 Docker 共用需注意目录冲突。

---

## 六、第四批实测（在线会话 / 风扇 / 备份 / 电源计划 / 日志 / 虚拟机）

> 验证时间：2026-10-26。所有 IP / 主机名 / 账号在本文档中均为占位值（`192.0.2.10` / `<用户名>`），
> 接口本身返回真实值——那是给用户自己看的。

### 1. 在线会话（`POST v1/user/online/list`）

| 项 | 值 |
|---|---|
| 方法 | **只吃 POST**，GET 返回 `9404` |
| body | `{"uid_list": [], "user_type": "user", "page": 1, "size": 50}` |
| 分页 | **`page` 从 1 开始**（与 `log/query` 相反） |
| 返回 | `{total, page, size, list:[...]}` |

`list[]` 字段（实测）：

| 字段 | 含义 | 实测值示例 |
|---|---|---|
| `id` | 会话 ID（踢人时用） | `203` |
| `username` / `uid` | 账号 | `<用户名>` / `1003` |
| `ip` | **客户端来源 IP** | `192.0.2.10` |
| `host` | 接入时用的 NAS 侧地址 | `192.0.2.10`（同批 Web 会话全同值） |
| `login_time` | 登录时间，Unix 秒 | `1792976494` |
| `connection_duration` | 已连接秒数 | `249` |
| `platform` | 客户端类型 | `2`=iOS `4`=Android `5`=- `6`=TV `7`=HarmonyOS `8`=HyperOS `9`=PC/WEB `10`=车机 `11`=HDMI |
| `protocol` | 协议 | `0`=HTTP `1`=HTTPS `2`=SMB `3`=FTP(S) `4`=SFTP `5`=SSH `6`=- |
| `connect` / `login_status` | 是否仍连着 / 登录态 | `true` |
| `is_current_connection` | 是不是"当前这条" | `true` 仅一条 |
| `pid` | SSH 会话的进程号 | `"3326367"` 或 `""` |

- 踢人：`POST v1/user/online/stop` 传 `{"id": <会话 id>}`，**前端只对 protocol 0/1（HTTP/HTTPS）开放**。
  中间层**未实现**（强制下线属高危操作）。
- 实测观察：反复登录会累积会话记录（脚本反复登录后本机列表涨到 23 条，`connect` 恒 `true`），
  NAS 不会立即清理未正常登出的会话。中间层照实返回，不过滤也不臆断。

### 2. 登录审计（没有独立接口）

猜过的路径全部不存在：`v1/user/login/record`、`v1/user/login/list`、`v1/user/session/list`、
`v1/user/online/history` 都是 `9404`；`v1/session/list`、`v1/log/login`、`v1/security/login/*` 是 **HTTP 404**。

唯一数据源是审计日志 `GET v1/log/query?module=login`：

| 场景 | level | content 形态 |
|---|---|---|
| Web/App 登录成功 | `info` | `User [<用户名>] from [192.0.2.10] logged in successfully.` |
| Web/App 密码错误 | `warning` | `User [<用户名>] from [192.0.2.10] failed to log in due to incorrect username or password.` |
| SSH 登录成功 | `info` | `User <用户名> logged into the system via SSH.`（**没有 IP**，operator 是 `system`） |

> 失败形态是用**一次**受控的错误密码登录实测出来的（先确认 NAS 自带 faillock 是「5 分钟 6 次」，
> 1 次远低于阈值，不会锁账号）。本机 2500+ 条日志里只有这一条失败记录。

`faillock`（只读，中间层不改）：

| 接口 | 返回 |
|---|---|
| `GET v1/security/faillock/config` | `{"status":1,"unlock_unit":"minute","faillock":{"deny":6,"fail_interval":5,"unlock_time":5}}` |
| `GET v1/security/faillock/blocklist` | `{"result":[]}` |
| `GET v1/security/faillock/whitelist` | `{"result":[]}` |

### 3. 风扇（只读）

| 接口 | 用途 |
|---|---|
| `GET v1/hardware/fan/config` | 当前策略与档位表 |
| `GET v1/taskmgr/stat/overview` | 实际转速 `cpu_fan{speed,status}` / `device_fan{speed,status}` |
| `GET v1/hardware/fan/start?mode=<n>` | **改模式（写）**，未实现 |
| `POST v1/hardware/fan/setfan` | **下发自定义曲线（写）**，未实现 |

`fan/config` 实测返回：

```json
{"mode": 2, "result": 2, "support_custom": false,
 "has_sys_fan": false, "has_cpu_fan": false,
 "sys_fan_table": [], "cpu_fan_table": []}
```

- `mode` 枚举只有前端知道（`fanModal` 的预设列表）：**1=静音 2=自动 3=全速 4=自定义**。
  `result` 与 `mode` 实测同值，保留原字段。
- 转速实测：机箱 545 RPM、CPU 风扇 385~1483 RPM（空闲时 CPU 风扇会停到 0，`status` 仍为 1）。
- **目标转速拿不到**：所有端点都只有实际转速，没有 PWM / 目标值字段。
- **档位表拿不到**：`support_custom=false` 且两张表为空数组；前端逻辑里只有机型不在
  `["dxp4800"]` 白名单时才显示自定义曲线，DXP4800 系列只给三个预设。
- `has_sys_fan` / `has_cpu_fan` 均为 `false` 却同时有两路转速 —— 这两个标志**不能**用来判断风扇有无。

### 4. 同步与备份

| 接口 | 说明 |
|---|---|
| `GET v1/web/sync/task/list` | 任务列表，`{all_count, count, list:[]}` |
| `GET v1/web/sync/task/detail?…` | 单任务详情（本机无任务，未验证） |
| `GET v1/web/sync/log/task/list` | 执行历史，同样结构 |
| `GET v1/web/sync/log/task/summary` | `{"transferringCount":0}` |
| `GET v1/web/sync/warn/list` | 备份告警横幅 `{"log_level":1,"list":null}` |
| `GET v1/web/sync/connection/list` | 已配对客户端 `{result:[…]}` |
| `GET v1/web/sync/general/get` | 全局设置（保留份数、限速等） |

**查询参数即表字段**（踩坑 49）：传未知字段名会报 `9999 操作失败! no such column: xxx`。
用这个差异探得可用列：`id`(int) `task_name` `status` `protocol` `user` `uid`(int) `progress`(int)
`size`(int) `backup_type` `sync_mode` `backup_local_paths` `backup_remote_paths` `schedule_time`。

状态枚举（前端 `TaskStatus`，两套编号一致）：

| code | 含义 | 字符串形态 |
|---|---|---|
| 0 | 已完成 | `finished` / `partialSuccess` |
| 1 | 失败 | `error` |
| 2 | 扫描中 | `scanning` |
| 3 | 已暂停 | `paused` |
| 4 | 进行中 | `progress` |
| 5 | 未开始 | `unStart` |
| 6 | 等待中 | `waiting` |

其它枚举：`backup_type` 1=增量 2=镜像 3=多版本；`schedule_type` 1=自动 2=手动 3=计划。

> **本机 0 个任务**（`list` 为空），只有 1 个已配对客户端（手机相册备份，`sync_protocol=pic`）。
> 因此任务条目的字段名只能按上表 + 前端归一化逻辑推断，接口里标 `schema_verified=false`，
> 每条都带 `raw`。**没有为了测试去创建备份任务**。
> 写操作（`task/sync`、`task/delete`、`task/now`、`task/pause`）一律不实现。

### 5. 电源计划（只读）

**没有独立接口**：`v1/hardware/power/{plan,schedule,list,time_switch}` 全 `9404`，
`v1/power/{plan,list}` 是 HTTP 404；`v1/hardware/wol/config` 也是 `9404`。
计划表就在 `GET v1/hardware/power/config` 里（与硬盘休眠策略同一个接口）：

```json
{"enable_scheduled_power": true,
 "power_boot": true,
 "wake_on": true,
 "time_switches": [{"frequency": [7,1,2,3,4,5,6], "time": "08:00", "action": "open"}]}
```

- `frequency` 星期编号（前端 `convertDay` 语义）：**1=周一 … 5=周五，6=周六，7=周日**；
  `[1..5]` 显示「工作日」，`[6,7]` 显示「周末」，`[1..7]` 显示「每天」。
- `action` 实测 `open` / `close`，前端把它们显示成「开机 / 关机」。
- `power_boot` / `wake_on` 只能按前端字段名直译（通电启动 / 唤醒），
  本机语言包里取不到对应中文文案，**未改写语义**。
- 写接口 `v1/hardware/power/start` 会改计划，误改会让 NAS 不按预期启停，未实现。

### 6. 日志查询（`GET v1/log/query`）参数全集

| 参数 | 说明 |
|---|---|
| `page` | **0 起**（前端 `page: this.page - 1`），回显在 `cur_page`，也是 0 起 |
| `size` | 实测 500 仍可用 |
| `level` | 可重复：`?level=warning&level=critical`；只认 `info`/`warning`/`critical` |
| `module` | 可重复；取值见 `v1/log/usersandmodules` 的 `modules` |
| `operator` | 可重复；前端筛选器用它按账号过滤 |
| `keyword` | 正文关键词（英文有效，中文命中 0 条——日志正文本身是英文） |
| `from_time` / `to_time` | Unix 秒；给未来区间返回 0 条，确认是真过滤 |
| `order` / `order_param` | `up`/`down` × `create_time`/`level`/`module`/`operator` |
| 返回 | `{cur_page, total, log_list:[{log_id, level, module, operator, content, create_time}]}` |

`GET v1/log/usersandmodules` → `{"levels":["info","warning","critical"], "modules":[…11 个…], "users":[…]}`。

### 7. KVM 虚拟机（未安装）

`GET v1/desktop/module/list` 本机 **31 个应用，无虚拟机**（无 `com.ugreen.virtualmachine`、
名称也无「虚拟机」）。因此以下路径全部 **HTTP 404 非 JSON**（模块未挂载）：

```
v1/vm/list   v1/virtualmachine/list   v2/vm/list
v1/vm/info   v1/vm/snapshot/list      v1/kvm/vm/list
```

→ 中间层返回 `installed=false` 并列出探过的路径。**未实现** VM 启停 / 删除 / 快照回滚。

---

## 四之三、第四批踩坑（44~54）

44. **`v1/user/online/list` 只吃 POST**：GET 返回 `9404`（"模块内路由不存在"），
    容易误判成接口不存在。判断存在性必须 GET/POST 都试。
45. **两个分页参数的基准相反**：`v1/user/online/list` 的 `page` **从 1 开始**，
    `v1/log/query` 的 `page` **从 0 开始**（`cur_page` 回显同样 0 起）。
    混用会静默错一页，不报错。
46. **会话里的 `host` 不是客户端 IP**：同一批 Web 会话 `host` 全相同（是接入时用的 NAS 侧地址），
    `ip` 才是来源地址；SSH 会话两者相同。只展示 `host` 会看不到真实来源。
47. **登录失败的审计级别是 `warning` 而不是 `error`**：`level` 只认 info/warning/critical，
    传 `error` 直接 `8001 无效的输入参数`。中间层做了 `error→critical` 别名映射。
48. **登录审计没有独立接口**，只能读 `log/query?module=login`；且 SSH 登录的账号只出现在正文里
    （`operator=system`），只按 operator 过滤会漏。
49. **备份任务列表把查询参数当 SQL 列名**：传未知字段报 `9999 … no such column: x`。
    既是坑（乱传参数会 500 级失败），也是**枚举列名的手段**。
50. **风扇 `mode` 枚举只有前端有**：后端只返回数字，1=静音 2=自动 3=全速 4=自定义，
    来源是 `fanModal` 的预设数组，不在任何接口文档里。
51. **`has_sys_fan` / `has_cpu_fan` 为 `false` 也可能有风扇**：本机两路转速都在报，
    这两个标志不能当"有没有风扇"用。
52. **`v1/hardware/fan/start` 是 GET 却是写操作**（改风扇模式），
    别用 HTTP 方法判断危险等级；同样的还有容器启停。
53. **电源计划没有独立端点**，藏在 `hardware/power/config` 里，和硬盘休眠策略共用一个接口。
54. **未安装的应用整个模块都是 HTTP 404 非 JSON**（虚拟机），
    与"模块已挂载但路由不存在"的 `9404` 可区分——这是判断"应用是否安装"最可靠的信号。

---

## 四之四、文件管理踩坑（55~66，2026-10-26 实测）

55. **`getRootDirs` 不返回对象，只返回数字**：`{root_list: [2,3,4,6,9]}`。
    数字到分类名的映射（1 收藏 / 2 个人 / 3 共享 / 4 用户 / 5 远程 / 6 设备 / 7 镜像 /
    8 分享给我 / 9 标签）只存在于前端 `initCache()` 里，任何接口都不返回。
56. **根级用 `root_type`、子路径用 `path`，两者互斥**。
    根级传 `path` 或不传参数都是 `1301`，容易误判成"没有权限"。
57. **卷根路径是禁区**：传 `path=/volume1` 一定 `1301 不允许访问`。
    账号能访问的最小单位是**共享文件夹根**（如 `/volume1/docker`）或家目录（`/home/<user>`）。
    要拿卷级容量只能用 `getVolumes`，不能靠列目录。
58. **共享文件夹根要额外传 `permission=4`**，否则返回空 `files`，且**不报错**。
    对应前端逻辑 `category === COMMON && level === Level1 ? 4 : 0`。
59. **`top_path` 是树的根，不是当前目录**。列 `/home/<user>/Music` 时它照样返回
    `/home/<user>`。拿它当"我在哪"会导致面包屑永远停在最外层。
60. **`file_type` 的 0 是"文件"不是"未知"**。标签根（`root_type=9`）返回的条目**根本没有**
    `file_type`，用 `file_type === 0` 判断"是文件"会把标签当成可下载的文件。
61. **下载令牌与会话令牌不通用**：第三步 `downloadFile` 只认 `dl_token`，
    带会话 `token` 会失败。而这个端点**在 `ugreen/` 前缀下**，并且前端关掉了统一鉴权。
62. **`createFolder` 的 `path` 是目标目录本身**，不是父目录。
    传父目录不会报参数错，而是因为"目标已存在"返回 `1327 存在同名文件夹`——
    看起来像"名字冲突"，实际是参数语义搞错了。
63. **`delPaths` 的 `paths` 只能是字符串数组**。传条目对象数组返回 `9999 操作失败!`，
    和"文件被占用"、"没有权限"的表现完全一样，极难定位。
64. **下载不存在的文件返回业务码 `499`**（不是 404），且是在**第三步**才报，
    前两步（建任务、换令牌）都会成功。所以不能靠第 1 步的结果判断文件是否存在。
65. **HTTP 层没有 `Content-Length`**：下载响应是 chunked。
    想在客户端做进度条只能用已下载字节数，不要指望 `expectedContentLength`。

### 中间层踩坑（66）

66. **流式转发必须在响应头发出之前判定上游状态**。
    先 `return StreamingResponse(...)` 再在生成器里 `raise HTTPException`，
    客户端拿到的是 `incomplete chunked read` / `peer closed connection`，而不是结构化错误。
    正确做法是 `await ctx.__aenter__()` → 检查 `status_code` → 不合格就读完 body、
    退出上下文、抛异常；合格才把已经进入的上下文交给生成器，`finally` 里退出。

## 四之五、上传踩坑（67~71）

67. **`change_time` 必须是 int64 unix 秒**。传 `"2026-10-26 10:00:00"` 这种格式化字符串，
    NAS 会用 Go 的反序列化错误回你：`json: cannot unmarshal number "" into Go struct field
    UploadFileHeaderParamInfo.ChangeTime of type int64`。前端那个 `Mh()` 就是转秒，不是格式化。
68. **`ug-param` 里的 `file_name` 和 `dir` 必须 URL 编码**。HTTP header 只能承载 ASCII，
    中文名不编码会在客户端就报 `UnicodeEncodeError`（或被 NAS 当乱码）。
    注意只编码这两个**值**，不要把整个 JSON 编码。
69. **`action_type` 0/1/2 都是静默覆盖**。想"保留原文件"只能用 `3`（改成 `名字(1).后缀`）。
    这是最容易造成不可逆数据丢失的一个参数，客户端默认值必须是 3。
70. **端点版本是 `v1` 不是 `v2`**：`v1/filemgr/fileUploadV2`。
    路径里带 `V2` 字样但版本段是 `v1`，`v2/filemgr/fileUploadV2` 不存在。
71. **预检端点不适合当"文件是否存在"的探针**：`v1/filemgr/fileUpload` 传空 body 返回
    `1005 参数错误 request message format error`，因为它的正常形态是 multipart。
    判断同名文件直接列目录即可（`getDirFileListV2`）。

## 四之六、容器 Web 访问踩坑（72~76，2026-10-26 实测）

72. **`portMapping` 里的 `nasPort` 类型不稳定，同一台设备上混用**。
    实测同一份列表里，有的容器给 `"666"`（字符串），有的给 `8090`（数字），
    而 `GetContainerById` 和 `ShowContainerDetailListV2` 两个接口都是这样。
    Swift 侧用 `Int` 直接解会整个列表解码失败；必须先试 `Int`、再试 `String`、再试 `Double`。
73. **`ContainerListV2` 完全没有端口字段**。它只返回 9 个基础字段
    （`application / containerId / createAt / imageId / imageName / name / projectName / status / version`）。
    端口在 `ShowContainerDetailListV2` 里，好在后者一次能取全部容器，不用逐个查详情。
74. **端口通不代表能当网页打开**。实测 16 个容器里只有 1 个（13888）返回 `200 + text/html`；
    `mc:25565` 是 Minecraft 纯 TCP 协议，塞进 WebView 只会白屏。
    打开前必须探测，且要能区分「端口不通」和「端口通但不是 HTTP」
    （`URLError.cannotParseResponse` / `badServerResponse` 是后者）。
75. **`GetContainerAccessLinkInfo` 的参数是宿主端口，不是 containerId**，
    且返回的是**规则配置**而非现成 URL。`urlPath` 容易漏——有些应用的 UI 挂在子路径下，
    只拼 `http://host:port/` 会得到 404。`httpsPort` 也可能和 http 端口不同。
76. **WKWebView 不实现 JS 弹窗回调 = 页面按钮点了没反应**。
    容器面板（青龙、qBittorrent、Halo 后台）大量用 `alert` / `confirm` / `prompt`，
    WKWebView 默认不显示，而且 `completionHandler` 不调用会让页面**永久卡死**。
    同理 `createWebViewWith` 不实现，`target="_blank"` 链接点了也没反应。
    这三类（弹窗、新窗口、下载）是容器面板在 WebView 里"能看不能用"的三个主要原因。


## 四之七、回收站与删除语义（77~80，2026-10-26 实测）

77. **`forever=false` 不保证进回收站，删了可能就没了**。
    决定权在**条目自身的 `recycle_open`** 字段上，不在请求参数上：

    | 条目字段 | 实测值 | 含义 |
    |---|---|---|
    | `recycle_open` | `false` | 该目录不支持回收站，删除即永久 |
    | `recycle_in_path` | `false` | 该路径下无回收站目录 |
    | `recycle_status` | `0` | 非回收站条目 |
    | `remaining_del_time` | `null` | 回收站剩余保留时间，非回收站条目为空 |

    实测流程：在共享目录建 `probe.txt` → `delPaths {forever: false}` → 文件立刻从目录消失
    → 回收站列表 0 条。同时 `getRecycleConf` 返回 `{"desktop_recycle": true, "dir_recycle": false}`。

    UGOS 网页端同样按此判断（前端源码）：
    ```js
    const s = !e[0].recycleOpen;   // 没开回收站 → true
    this.delPaths(e, r || s)       // 强制 forever=true
    ```
    **所以客户端不能对用户承诺「可以找回」，必须读 `recycle_open` 再决定文案。**

78. **`delPaths` 的返回值不是任务 id**。成功时返回 `{"result": ""}`（空字符串），
    不像上传/下载那样给 uuid 让客户端轮询进度。把它当异步任务去 `getDelTaskInfo` 会一直查不到东西。

79. **回收站列表复用 `getDirFileListV2`，不是独立接口**。请求体里加 `is_access_recycle: true`，
    并按条目的 `is_access_recycle` 语义走同一套分页。回收站内搜索另有
    `createRecycleSearchTask` / `getRecycleSearchTaskResult` / `cancelRecycleSearchTask`。

80. **回收站目录不存在时回 `1375`（新错误码）**。`root_type=2` 调回收站列表得到
    `1375 回收站目录不存在`，而 `root_type=3` 正常返回（空列表）。
    `getRecycleRootDirs` 返回的 `root_list` 只是「支持回收站的根分类」，
    不代表每个根下真的建了回收站目录。

### 回收站相关接口清单（备查，本轮未接入）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `v1/filemgr/getRecycleConf` | `{desktop_recycle, dir_recycle}` 开关状态 |
| GET | `v1/filemgr/getRecycleRootDirs` | `{root_list: [2,3,4]}` 支持回收站的根分类 |
| POST | `v1/filemgr/restoreRecycle` | **恢复文件**，body 为路径数组 |
| POST | `v1/filemgr/emptyRecycleBin` | 清空回收站 |
| POST | `v1/filemgr/switchRecycle` | 开/关回收站 |
| POST | `v1/filemgr/setRecycleConf` | 改回收站配置 |
| POST | `v1/filemgr/addPlan` / `editScheduledTask` / `rmPlan` | 定时清理计划 |
| POST | `v1/filemgr/createRecycleSearchTask` / `getRecycleSearchTaskResult` / `cancelRecycleSearchTask` / `removeRecycleSearchTask` | 回收站内搜索 |


## 四之八、缩略图与媒体预览（81~84，2026-10-26 实测）

81. **缩略图接口完全忽略 `width` / `height`**。
    `GET v1/filemgr/thumbnail?path=&type=&size_type=0&mtime=&ctime=&file_size=&width=&height=`
    看似能指定尺寸，实测**无论传多少都回同一张图**：

    | 请求 width | HTTP | 返回 |
    |---|---|---|
    | 16 | 200 | JPEG **112×96**，1017 字节 |
    | 48 | 200 | 同上 |
    | 88 | 200 | 同上 |
    | 120 | 200 | 同上 |
    | 256 | 200 | 同上 |
    | 512 | 200 | 同上 |
    | 1024 / 2048 | 200 | 同上 |

    源图是自造的 1600×1200 PNG。UGOS 前端自己传的也是固定预设
    `[16, 120, 88, 48]`（选中态/普通态之类的档位），说明服务端只认预设档，
    非法值落回同一个默认缩略图。
    **结论：这接口只配做列表小图标，别指望它出大图。**

82. **首次请求回 `HTTP 423`（Locked），body 是 `thumbnail is generating asynchronizely`**。
    不是错误码体系里的东西，是 HTTP 状态直接 423。实测要 2~3 秒，
    重试 2 次左右才 200。生成后 NAS 侧有缓存，同一文件再取直接 200。
    重试不要少于 6 次 × 0.8 秒。

83. **返回体统一是 JPEG**，哪怕源文件是 PNG（实测 1600×1200 PNG 拿回来是 JPEG）。
    所以不能靠原文件后缀判断 content-type。

84. **大图预览走 `GET v1/filemgr/mediaPlayer`，不用换 `dl_token`**。
    参数形状和 thumbnail 几乎一样，多一个可选的 `code=1`（实测带不带都行）：

    ```
    GET v1/filemgr/mediaPlayer?path=&type=1&size_type=0&mtime=&ctime=&file_size=
        &intranet_share_id=0&width=1920&height=1080&token=<会话 token>
    ```

    实测：1600×1200 PNG 源文件 37334 字节，拿回来**正好 37334 字节，一字节不差**，
    `content-type` 也照实回 `image/png`。

    对比三条取字节的路子：

    | 通道 | 换 token | 返回 | 适合 |
    |---|---|---|---|
    | `thumbnail` | 不需要 | 112×96 JPEG | 列表小图标 |
    | `mediaPlayer` | **不需要** | 源文件本体 | 大图预览 |
    | `downloadFile` | 需要三步换 `dl_token` | 源文件本体 | 落盘存档 |

    `mediaPlayer` 是预览的最短路径。但**没有验证 Range 支持**，
    视频拖动播放能不能用它还是未知数，播视频目前仍建议走下载链路。

### 由上述结论落地的代码

| 位置 | 做了什么 |
|---|---|
| `ios/NasManager/Core/UgosClient.swift` | 新增 `getBinary`：不走 JSON 解析，423 退避重试，会话过期自动重登 |
| `ios/NasManager/Core/FileService.swift` | `thumbnail(for:)` 列表缩略图、`previewBytes(for:)` 原图通道 |
| `ios/NasManager/Views/FileThumbnail.swift` | 内存缓存（`path\|mtime\|size` 做键）+ 3 路并发闸门，失败静默退回系统图标 |
| `ios/NasManager/Views/ImagePreviewView.swift` | 全屏预览，双指缩放 / 双击切换 / 仅放大后可拖动，>64MB 直接劝退 |
| `server/app/services/files.py` | `thumbnail()` 修正尺寸语义、`preview()` 新增 `mediaPlayer` 通道 |
| `server/app/api/routes.py` | `GET /files/thumbnail`、`GET /files/preview` |
| `scripts/test_media.py` | 22 项离线断言（请求形状 / 423 重试 / 路径安全） |

## 四之九、复制 / 移动（85~88，2026-10-26 实测）

> 文件管理器最基础的缺口。App 之前只能新建 / 改名 / 删除 / 上传 / 下载。

85. **`cpOrMvPath` 的 `src` 必须是字符串路径数组**。
    ```json
    POST v2/filemgr/cpOrMvPath
    {"dst": "/volume1/docker/sub", "src": ["/volume1/docker/a.txt"],
     "action_type": 3, "type": false, "intranet_share_id": 0}
    ```
    传对象数组 `[{"path": "...", "intranet_share_id": 0}]`（像 `addPathsTask` 那样）
    直接得到 **`9999 操作失败!`**。这点和 `delPaths` 一致。

86. **`type` 是 `true` = 移动，`false` = 复制**（不是反过来）。

87. **`action_type` 枚举**（从 `apps_filemgr` 的 `skip=0, cover=1, save=3, askEachTime=4` 反出，实测验证 3）：

    | 值 | 含义 | 实测行为 |
    |---|---|---|
    | 0 | skip 跳过 | 同名时不动作 |
    | 1 | cover 覆盖 | 同名时替换 |
    | 3 | save 另存 | 生成 `hello(1).txt`、`hello(2).txt` |
    | 4 | askEachTime | 逐项询问（前端弹窗用） |

    **默认该用 3**：既不覆盖也不丢数据。

88. **复制 / 移动是异步任务，而且查不到进度**。返回 `{"path": "", "task_id": "<uuid>"}`。

    实测时序：复制 0.7 秒完成，移动 0.1 秒完成（都是几百字节小文件）。

    但**没有任何可用的进度接口**：

    | 查询方式 | 结果 |
    |---|---|
    | `getDelTaskInfo?task_id=<id>` | `9999 操作失败!` |
    | `getDelTaskInfo?task_uuid=<id>` | `9999 操作失败!` |
    | `getDelTaskInfo?uuid=<id>` | `1368 对象访问超时，您的任务不存在或已结束` |

    `1368` 是新见到的错误码。**结论：客户端只能轮询目标目录判断落地**，
    别去任务中心找这类任务。大文件的耗时未测（本机没有大文件样本），
    轮询超时建议留足（代码里给 30 秒 × 1 秒间隔）。

### 落地

| 位置 | 做了什么 |
|---|---|
| `server/app/services/files.py` | `copy_or_move()`：字符串数组校验、拒绝「移动到自己的子目录」、`action_type` 常量 |
| `server/app/api/routes.py` | `POST /files/transfer` |
| `ios/NasManager/Core/FileService.swift` | `copyOrMove(paths:to:move:)` + `waitForTransfer()` 轮询落地 |
| `ios/NasManager/Views/DestinationPickerView.swift` | 目标目录选择器，只列目录，被操作条目自身不可选 |
| `ios/NasManager/Views/FilesView.swift` | 左滑「复制 / 移动」+ 右键菜单「复制到… / 移动到…」 |
| `scripts/probe_copy_move.py` / `probe_copy_move2.py` | 真机契约探针（沙盒目录，测完删除） |

## 四之十、压缩 / 解压（89~91，2026-10-26 实测）

89. **压缩 `POST v1/filemgr/compressedFile`**：

    ```json
    {"name": "probe", "ext": "zip", "conflict_options": 0,
     "out": "/volume1/docker/src", "paths": ["/volume1/docker/src/note.txt"]}
    ```

    - `name` **不含后缀**，后缀由 `ext` 决定
    - `out` 是输出目录
    - 返回 `{result: "<uuid>"}` = 异步任务
    - 实测 0.6 秒出包，生成 `probe.zip`
    - `conflict_options` 的取值语义**没有验证**（本轮测的是不撞名的场景），
      所以客户端应自己先把包名错开，别赌它不会覆盖

90. **是否需要密码 `POST v1/filemgr/isEncryptZip {path}`** → `{result: false}`。
    `result` 为真时解压必须带 `password`。

91. **解压 `POST v1/filemgr/unCompressedFile`**：

    ```json
    {"out": "/volume1/docker/out", "path": "/volume1/docker/src/probe.zip",
     "action_type": 0, "password": "<加密时才带>"}
    ```

    - 返回 `{result: "<uuid>"}` = 异步任务，实测 0.6 秒解完，内容正确
    - `out` **需要是已存在的目录**，调用方要先建（已存在回 `1327`，忽略即可）
    - `out` 留空时 UGOS 网页端的习惯是解到「同名目录」（去掉后缀）
    - 支持的后缀（前端静态表 `O0`）：`zip / rar / 7z / tar / gz / tgz / iso`

92. **`getArchiveFileInfo` 调不通**：`v2/filemgr/getArchiveFileInfo` 无论传
    `{path}` 还是 `{paths:[path]}` 都回 `9404`。前端配置里它带
    `whitListCode: [1360]`，说明正常路径下也可能回 `1360`。
    拿压缩包内文件清单这条路暂时走不通，不影响压缩 / 解压本身。

### 落地

| 位置 | 做了什么 |
|---|---|
| `server/app/services/files.py` | `compress()` / `is_encrypted_archive()` / `extract()`，新增 `ARCHIVE_EXTS` |
| `server/app/api/routes.py` | `POST /files/compress`、`POST /files/extract` |
| `ios/NasManager/Core/FileService.swift` | `compress` / `isEncryptedArchive` / `extract` + `waitForArchive()` 轮询落地 |
| `ios/NasManager/Views/FilesView.swift` | 菜单「压缩成 ZIP」/「解压到同名目录」，加密包弹密码输入框 |
| `scripts/probe_archive.py` | 真机闭环探针（建沙盒 → 压 → 解 → 校验 → 全删） |

## 四之十一、文件夹体积（93，2026-10-26 实测）

93. **`GET v2/filemgr/getSizeAndCount` 必须带 `intranet_share_id`**。

    | 请求 | 返回 |
    |---|---|
    | `?path=/volume1/docker` | `{size: 0, file_count: 0, dir_count: 0, status: 0, calculate_id: "..."}` |
    | `?path=/volume1/docker&intranet_share_id=0` | `{size: 749719225, takes_space: 754683904, file_count: 2099, dir_count: 310, ...}` |
    | `?paths=["/volume1/docker"]` | `9999 操作失败!` |

    只传 `path` 看着"成功"，实际所有数字都是 0，像空目录——**这是最容易误判的坑**。

    另外**计算是增量的**：同一个目录连打 3 次，`size` 从 749MB →
    1.35GB → 1.35GB，`file_count` 从 2099 → 2690 → 2737。

    `status` 一直返回 `0`，**没有明确的「算完了」标志**。
    所以客户端只能连续采样到数字不再变化。大目录别指望一次就准。

    字段含义：`size` 逻辑大小，`takes_space` 实际占用（含块对齐），
    `file_count` / `dir_count` 文件与子目录数，`calculate_id` 是这次计算的 id
    （`delSizeAndCount` 用来取消，未验证）。

### 落地

| 位置 | 做了什么 |
|---|---|
| `server/app/services/files.py` | `folder_size()`：强制带 `intranet_share_id` |
| `server/app/api/routes.py` | `GET /files/size` |
| `ios/NasManager/Core/FileService.swift` | `folderSize()` + `measureFolder()`（采样到稳定） |
| `ios/NasManager/Views/FileInfoSheet.swift` | 条目详情：目录现算体积，算的过程中显示「正在计算…」 |
| `scripts/probe_folder_size.py` | 6 种载荷形状逐一试，确认只有带 `intranet_share_id` 才真算 |
