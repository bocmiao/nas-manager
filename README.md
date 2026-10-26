# NasManager

绿联（UGREEN）NAS 的原生 iOS 管理客户端，直接对接 UGOS Pro 的私有 API。

官方「绿联云」App 的问题不是功能少，而是该有的不好用：告警不能自定义、容器日志看不到、存储健康只给一个绿点、RAID 无冗余不提醒、外网访问绕中转。这个项目把这些补上。

> **非官方项目**。接口来自对 UGOS Pro Web 前端的逆向分析，属于未公开 API，固件升级后可能失效。仅供自有设备管理与技术研究使用，请自行评估风险，遵守你所在地区的法律法规与设备服务条款。

## 项目包含什么

| 部分 | 说明 |
|---|---|
| **iOS App** | SwiftUI 原生客户端，App 直连 NAS，不走中间层。iOS 17+ |
| **中间层（可选）** | FastAPI 服务，把 UGOS 数据归一化 + 告警状态机 + APNs 推送。适合想接 Webhook / 推送，或多设备共享的场景 |
| **契约文档** | `docs/api-verified.md`：逐条对着真机验证过的接口契约、错误码表、54 条踩坑记录 |

## 功能

**iOS 客户端**

- **总览** — 设备信息、CPU / 内存 / 风扇、网口实时流量、存储池与卷、磁盘温度、容器摘要、**历史曲线**（Swift Charts，CPU·内存·温度·网口，可切 30 / 120 / 360 采样点）、固件更新提示条、UPS 状态卡（未接则整块隐藏）
- **容器** — 列表与筛选、启动 / 停止 / 重启、日志查看（关键词过滤 + 行数切换）；**镜像管理**（列表 / 删除 / 拉取并跟踪进度）
- **文件管理** — 按 NAS 的根分类浏览（个人 / 共享 / 用户 / 外部设备 / 标签），面包屑下钻、按名称·大小·修改时间排序、分页加载；**上传**（同名默认自动改名，不覆盖）、下载（流式转发）、新建文件夹、重命名、删除（默认进回收站）；图标按后缀自动分组
- **存储** — 存储池（RAID 级别与冗余状态）、卷与分区、磁盘 SMART 明细（健康 / 温度 / 通电时长 / 关键属性）、**快照管理**（按文件夹列出 / 创建 / 删除 / 回滚，不支持时如实说明原因）
- **告警** — 8 条本地规则可开关与调阈值，事件列表、未读角标、本地通知
- **设置** — 多台 NAS 配置管理（切换 / 新增 / 编辑 / 删除，旧单地址配置自动迁移）、内网 + 公网双地址自动切换、密码按配置存 Keychain、TOFU 证书指纹校验、Face ID / Touch ID 解锁、刷新间隔、固件版本检查
- **桌面小组件** — 小 / 中尺寸展示 CPU·内存·温度·容器数·告警数，数据超 10 分钟标「可能过期」
- **Siri 快捷指令** — 「查询 NAS 状态」「查看 NAS 告警」，读小组件共享快照
- **IPv6** — 地址支持 IPv6 字面量，自动规范化为 `https://[addr]:port`

**中间层**

- 60 个路径 / 67 个操作（OpenAPI 实测），覆盖系统、监控、存储、容器、镜像、快照、日志、告警、推送、用户、会话、风扇、备份、电源
- 23 条告警规则，状态机去重（只在 raised / resolved 跳变时发事件）
- **会话与登录审计**：在线会话（用户 / 来源 IP / 登录时间 / 协议）、登录审计（分页、只看失败）、失败按 IP 聚合、faillock 锁定策略
- **风扇**：策略模式、机箱 / CPU 转速；目标转速与档位表在真机上不可读，接口显式标 `read_only`
- **备份与同步**：任务列表（状态 / 进度 / 上次 / 下次）、失败任务、超期未运行任务
- **电源计划**：只读查询开关机时间表与下次执行时间，不提供写接口
- **系统日志**：级别多选、时间范围、关键词、分页
- 磁盘测速异步任务、SMART 明细、容器日志解帧、Docker 资源、进程与服务列表、容器详情
- **历史采样**：60 秒一采、保留 360 点的环形缓冲，供曲线查询（含风扇转速）
- APNs 推送通道（未配凭据时自动降级为日志）
- 单文件哨兵 `scripts/sentinel.py`：仅标准库，用于「NAS 本身挂了」这种中间层无法自救的场景

## 为什么没有云服务器中间层

早期方案里有个云服务器做中转。砍掉了：

1. NAS 有公网 IPv4 时，App 可以直连 9443，不需要中转。
2. 中间层天然是单点故障：NAS 挂了，跑在 NAS 上的告警发不出去。
3. 多一跳就多一个要维护、要防护、要付费的东西。

所以中间层**降级为可选组件**——想用可以 Docker 部署，但它不在关键路径上。

### 外网访问

直连，不套任何穿透层：

```
外部 <高位端口>/tcp  ──路由器端口映射──>  <NAS 内网地址>:9443
```

几条硬要求：

- **不要映射 9443 本身**，换高位端口。9443 是 UGOS 默认管理口，扫描器常客。
- **上 DDNS**。家宽公网 IP 会变，写死会失效。
- **强密码 + 保持登录失败锁定**。
- **不开 UPnP**，让路由器映射是唯一入口。

风险等价于「把 NAS 管理口挂公网」。要更高安全性，自建 WireGuard（手机需装客户端），或放弃公网直连。

## 目录

```
docs/api-verified.md        实测接口契约（核心资产）
docs/ugos-ios-app-plan.md   调研：能力地图、用户反馈、功能机会矩阵
ios/                        SwiftUI App（XcodeGen，iOS 17+）
server/                     可选中间层（FastAPI）
scripts/                    逆向工具链 + 构建脚本 + 哨兵
```

## 快速开始

### iOS App

```bash
cd ios
xcodegen generate
open NasManager.xcodeproj
```

没有 Mac 就用 CI 出包，或本地跑同一个脚本：

```bash
./scripts/build-ios.sh          # 无签名 IPA，用 AltStore / Sideloadly 自签安装
./scripts/build-ios.sh --adhoc  # Ad-Hoc 签名，需本机已装证书与描述文件
```

CI 走 `.github/workflows/ios-build.yml`（macOS runner，产物留在 Actions artifacts）。

### 中间层（可选）

```bash
cp .env.example .env    # 填 NAS 地址与账号
cd server
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8787
```

接口文档在 http://127.0.0.1:8787/docs

### 哨兵

```bash
python scripts/sentinel.py --host <NAS 地址> --token <中间层 token> \
  --webhook https://example.com/hook
```

## 接口要点（逆向所得，易踩）

1. `GET /ugreen/v1/verify/check` 需带 `{"username": ...}`，**公钥在响应头 `X-Rsa-Token`**，不在 body。
2. 密码用 RSA 分段加密（`chunk = 245`，base64 拼接后提交）。
3. token 走 **query**（`?token=xxx`）。放 header 会返回 `9404`。
4. 路径前缀 `ugreen/` 是**强制**的，漏了会得到空 body 的 404，很容易误判成端点不存在。
5. 磁盘 `status` 的 `0` 和 `1` 都表示正常（`1` = 已激活在用），只把其他值当异常。
6. SMART 查询的 `disk` 参数必须传 `dev_name`（`/dev/sda`），传裸名或序列号返回 `5001`。
7. 容器启停是 **GET** 带 `?containerId=`，删除才是 POST。
8. 在线会话 `v1/user/online/list` **只吃 POST**（GET 返回 `9404`），`page` 从 1 起；而日志 `v1/log/query` 的 `page` 从 0 起。
9. 判断某个应用是否安装，看整个模块是否返回**非 JSON 的 HTTP 404**（KVM 虚拟机即如此），别只看单个接口的 `9404`。

完整清单见 `docs/api-verified.md`。

## 已知限制

- **快照需要 Btrfs**。UGOS 只对 Btrfs 卷开放快照，ext4 卷上列表返回 `32003`、创建/回滚返回 `not btrfs filesystem`。客户端会如实显示「该存储不支持快照」，不会显示空列表冒充成功。
- **容器 compose 内容拿不到**。`GetContainerById` 等 23 个字段里没有 compose / yml，docker 应用 87 个端点全量枚举也无相关接口；compose 文件只存在于磁盘路径 `/volume1/@appstore/...`，不通过 API 暴露。
- **镜像拉取没有百分比**。拉取期间服务端只给 `status` 状态码，无进度值，客户端显示的是阶段而不是进度条。
- **KVM 虚拟机**：真机未安装该应用，`desktop/module/list` 里没有对应模块，6 个候选路径全部返回 HTTP 404。接口按探测结果返回 `installed: false` 并附探测细节，不提供启停 / 删除 / 快照回滚。
- **风扇目标转速与档位表读不到**。`v1/hardware/fan/config` 只给 `mode` 与 `support_custom`，两张档位表在真机上恒为空；接口返回 `target_rpm.supported: false` 并说明原因，不猜数值。
- **备份任务的列结构未在真机充分验证**。本机 0 个备份任务，列名靠「传未知字段回 `no such column`」枚举得到，接口用 `schema_verified: false` 如实标明。
- `volume.read_rate` 单位存疑（实测量级不稳定，疑似采样窗口累计字节而非 B/s），客户端不渲染该值
- 磁盘测速对存储池内的磁盘会被 UGOS 拒绝（`disk is busy`），需要空闲盘
- UPS 字段在未接 UPS 时恒为 `null`，已接分支的字段名未经真实设备验证
- 进程 / 服务启停接口存在但属高危写操作，本期只做只读
- 中间层的历史采样是内存缓冲，服务重启即清空（响应里用 `since` 标明起点）
- iOS 端依赖 iOS 17+（使用了 Observation 框架）
- **主屏小组件依赖 App Group**。Widget 与主 App 分属两个进程，数据只能经 `group.dev.nasmanager.shared` 共享。无签名构建下拿不到该 Group（`UserDefaults(suiteName:)` 会退回本地域），小组件只会显示「未获取到数据」占位，不会崩也不会显示假数值。要真正生效需在自签时给两个 target 都配置 App Group 权限，并使用包含该 Group 的 provisioning profile。

- **上传不支持断点续传**。NAS 有完整的续传协议（`fileUpload` 预检 + `getUpdateTmpInfo` 查已传 + `delUpTemFile` 清临时文件），但只验证了「从头传」这一条路径；中断后重传会从头开始。分块本身是追加语义，所以多块大文件是可靠的。
- **上传的 `action_type` 默认必须显式指定**。NAS 的 0/1/2 三个值都会**静默覆盖**同名文件，只有 3 会改成 `名字(1).后缀`。本项目统一用 3，且在 UI 上写明「同名时自动改名，不覆盖」——这是唯一不丢数据的选择。

## 开发

```bash
# 后端（需 Python 3.11+）
cd server && pip install -r requirements.txt
python -m uvicorn app.main:app --reload

# 逆向工具链
python scripts/nas_login_test.py <user> <password>   # 验证登录链路
python scripts/probe_endpoints.py                     # 批量探测端点
python scripts/probe_batch4.py all                    # 第四批探测：会话 / 风扇 / 备份 / 电源 / 日志 / 虚拟机
python scripts/test_alerts_batch3.py                  # 容器基线 / 服务 / 网口规则验证（41 项）
python scripts/test_alerts_batch4.py                  # 登录失败 / 备份 / 解析函数验证（55 项）
python scripts/test_degraded_snapshot.py              # 数据源降级：不误报恢复、reachable 判定（23 项）
```

凭据只从环境变量或 `.env` 读取，仓库内不含任何真实设备信息。

## 许可

MIT。与绿联（UGREEN）无关联，未获其认可或支持。「UGREEN」「UGOS」是其各自权利人的商标，仅用于说明兼容对象。
