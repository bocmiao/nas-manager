# UGOS Pro 实测接口契约（目标机验证通过）

> 设备：UGREEN DXP4800 Plus
> 固件：**UGOS Pro 1.19.1.0126**（Web 前端 1.19.0.78471，build 2026-08-19）
> 局域网：`https://<NAS 地址>:9443`（自签证书 CN/ORG = `UGREEN`）
> 验证时间：2026-10-25
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
| 5001 | 硬盘不存在（SMART/测速的 `disk` 参数值不对） |
| 9404 | 客户端版本/通道不匹配（POST 带 token 时常出现） |
| 9405 | 应用未安装或无权限 |
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
| GET | `v1/docker/container/GetContainerById?containerId=<id>` | 容器详情（环境变量、网络、端口、限制） |
| GET | `v1/docker/container/ShowCpu` / `ShowMemory` | **宿主机级** CPU/内存，非容器级（传 containerId 无效） |
| — | `v1/docker/container/AddTerminal` / `DelTerminal` | **容器内终端** |
| — | `v1/docker/image/ObtainAllImages` / `ShowLocalImageV2` / `DeleteImage` / `DownloadImage` | 镜像管理 |
| — | `v1/docker/view/GetEngineStatus` / `GetRegistryMirrors` / `SetRegistryMirrors` | Docker 引擎与镜像加速配置 |

### 文件管理

| 方法 | 路径 | 返回要点 |
|---|---|---|
| POST | `v1/filemgr/getDirFileList` | 目录列表（GET 会 9404） |
| GET | `v1/filemgr/getHomeFolderSet` | 家目录配置 |
| — | `v2/filemgr/getDirFileListV2` / `getRootDirs` / `rename` | v2 新接口（1.19.1 存在） |

---

## 三、应用清单与 SPA 入口

`GET /ugreen/v1/desktop/module/list` 返回所有已安装应用（`module_id` / `name` / `entry` / `version` / `app_status`）。

逆向某应用接口的方法：
```
GET https://<ip>:9443<entry>/            → HTML，提取 src="*.js"
GET https://<ip>:9443<entry>/js/*.js     → 正则提取 "v\d+/模块/动作"
```

本机已装应用（节选）：Docker、文件管理、存储管理、任务管理器、控制面板、日志中心、快照、下载中心、迅雷、同步与备份、影视中心、音乐、相册、在线文档、保险箱、文件版本管理、网盘工具、安全管家、应用中心、全局搜索、模型管理，以及 Docker 衍生应用 Lucky / 青龙 / qBittorrent / AI Plugins / 节点小宝。**未安装 KVM 虚拟机应用**。

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
