# 绿联 DXP4800 Plus · iOS 自建管理端 — 调研与实现方案 v2

> 目标：iOS 原生 App，补齐官方「绿联云」App 缺失的能力，提供接近 1Panel 的运维体验，但**不套壳任何面板**。
> 环境：DXP4800 Plus（x86_64 / UGOS Pro 基于 Debian / Docker 可用），IPv4 公网，付费 Apple 开发者账号。
> 更新：2026-10-25 v2 —— 新增「官方 App 用户评价」调研章节，并按痛点重排功能优先级。

---

## 一、GitHub 可复用成果

| 项目 | 语言 | 覆盖 | 实测版本 | 许可 | 用法 |
|---|---|---|---|---|---|
| `metaneutrons/ugos-cli` | Rust | 认证 / TLS / AES-GCM / KVM / Docker / 文件 / 下载 / 系统 / 快照，含 10 篇逆向文档 | 1.18.1（早期 1.14.1） | GPL-3.0 | 只读文档，按契约自行实现；不抄代码 |
| `hongjiahao371-pixel/ugos-pro-mcp` | Python | 25 个工具，含系统监控、存储卷、文件、日志、下载、影视刮削 | 1.19.1.0126 | 开源 | **直接改造为 HTTP 服务**，已处理 1.19.1 接口漂移 |
| `pewee-live/ugos_pro_api` | Java | 登录 / 会话 / 文件 | — | 开源 | 认证链路流程参考 |
| `lxm20060513-svg/qingliao-ios` | Swift | SwiftUI + XcodeGen + Actions 出 IPA + 侧载 | — | 开源 | iOS 工程脚手架参考 |
| `okxlin/docker-1panel` | — | Docker 方式跑 1Panel | — | 开源 | 仅作可选第二数据源 |

**UGOS 认证链路（三项目交叉验证）**
1. `POST /verify/check` → RSA 公钥
2. RSA **PKCS1v1.5**（非 OAEP）加密密码 → `POST /verify/login`
3. 返回 Cookie + `token_id`（→ `X-Ugreen-Security-Key`）+ 动态 `token`
4. 请求头带 `X-Ugreen-Token`（另一把 RSA 公钥封装）+ URL 带 `?token=`
5. 部分写操作请求体用 **AES-GCM** 包装
6. 凭据过期返回 `1024`，需静默重登
7. TLS 自签 X.509 v1 证书

**上传接口三要素（踩坑实录，缺一即静默失败）**
- token 三处齐全：URL 参数 + `X-Ugreen-Token` + `X-Ugreen-Security-Key`
- HTTP 头禁止非 ASCII：中文文件名需 RFC 5987 percent-encoding，`ug-param` JSON 需 `ensure_ascii=True`
- `ug-param` 中 `first_request` 必须为 `false`

---

## 二、UGOS Pro 完整能力地图

Base：`https://<nas-ip>:9443/ugreen/v1/`

### 已完整文档化
| 模块 | App ID | 能力 |
|---|---|---|
| Auth | core | RSA 换钥、登录、会话、自动重登 |
| System / monitoring | core | 型号/序列号/固件版本/CPU/内存/网卡、CPU·内存·磁盘·网络·风扇实时数据 |
| File Manager | com.ugreen.filemgr | 目录浏览、预上传、上传、下载、删除（可进回收站）、重命名、新建目录 |
| Docker | com.ugreen.docker | 容器、镜像、**Compose 项目** |
| KVM | com.ugreen.kvm | VM 列表/详情/启停/强制停止/重启/删除、**快照创建·回滚·重命名·删除**、网络、存储、镜像、USB |
| Download Center | com.ugreen.downloadCenter | 下载任务、添加 HTTP/磁力 |
| Filesystem Snapshot | com.ugreen.snapshot | 文件系统快照 |
| Backup | — | **无 API，做不了** |
| Request encryption / TLS | — | AES-GCM 包装、证书版本与 pinning |

### 已知端点、未文档化（提取自主 JS bundle）
Photo `photo/`（v5 前缀，90+ 端点）· Video `video/details/`（元数据、演员）· Music `music/` · Network `network/iface/`、`network/auth/` · User `user/`（列表已实现）· Log `log/`（查询已实现）· Firmware `firmware/`、`upgrader/` · Security `security/ssl/` · Sync `sync/syncthing/` · Hardware `hardware/ups/` · Time `time/config` · Office `office/` · Vault `vault/` · Assistant `assistant/chat/` · Help Center `helpcenter/` · Antivirus `antivirus/` · Editor `editor/` · Version Explorer `versionExplorer/` · Cloud Drives `baidu/`、`onedrive/`、`netDisk/` · Stream `stream/transcode/` · Connect `connect/` · Wizard `wizard/`

### 逆向方法（未来固件升级后自己补端点的路数）
UGOS 每个应用是独立 Vue SPA：`GET /photo/`、`/docker/` 等返回 HTML → 提 `assets/*.js` → 正则抓 `getServerUrl("/ugreen/...")` 与 `v\d+/模块/动作` 字符串。

---

## 三、官方 App 用户评价调研（2026-10-25 新增）

### 3.1 评分现状

| App | App Store（中国区） | 评分数 | 定位 |
|---|---|---|---|
| 绿联云 `id6458647408` | **4.0 / 5** | ~1200 | UGOS Pro 专用客户端（我们替代的对象） |
| 绿联私有云 `id1489890816` | **3.6 / 5** | 586 | 老 UGOS 客户端 |

4.0 是个"不算难看"的分数，但差评高度集中在**管理能力**与**通知**上，而不是界面美观度。

### 3.2 高频差评证据

**① 没有系统级推送 —— 最致命**

> 「不能在手机端 app 外推送通知吗？现在 iOS 上需要打开 app 后进入通知中心主动查看才可以」
> —— 绿联 NAS 社区 `thread-874`，用户 MrGlasses

> 「我这次硬盘被检测到坏道就没任何通知，只有一条异常日志，并且 SMART 检测任务执行失败，均没推送通知」
> —— 同上，用户 UVRzwi

> 「出现硬盘故障的日志时，无法第一时间告知用户，只有用户主动到日志中心查询才能看到。因为硬盘故障日志的操作者就是 system」
> —— 同上，问题起因

官方答复：系统层级信息「默认提醒给管理员角色」。但用户实测**收不到**。→ 这是唯一一条能直接威胁数据安全的产品缺陷。

**② 手机端管理能力残缺，必须开浏览器切「电脑模式」**

> 「绿联云 APP 功能对 NAS 的一些管理不是很全面，还得使用浏览器更换电脑模式进行登录，之后操作起来不是方便」
> 「很多功能设置 APP 上都没有，不方便管理」
> —— 社区 `thread-550`

> 「希望支持手机端编辑 docker-compose 文件」
> —— 同上，用户 UVRzwi 在帖子地板明确提出，**官方至今未做**

**③ Docker 在手机上只有「快捷访问」**

官方唯一路径：Docker → 容器 → 右侧 `···` → 访问 → 在手机浏览器里打开容器自己的 Web UI。
= 手机端**没有**容器启停、日志、资源占用、镜像管理、Compose 编排。

**④ 影视刮削本土化差**

> 「刮削源单一：目前数据源仅有 TMDB 和智能识别，缺少国内主流刮削源。导致库里的国漫、国产剧和综艺经常匹配不到准确的元数据和海报，后期手动修正工作量巨大」
> 「原生集成豆瓣（Douban）等国内优质数据源」
> —— 社区 `thread-4115`（影视中心 7 大痛点）

好消息：UGOS 内部已有豆瓣通道，`ugos-pro-mcp` 实测「豆瓣通道免 TMDB Key 实测有效」，可做手动纠错。

**⑤ 硬盘管理深度不足**

> 「只有基础的 S.M.A.R.T. 和健康状态，没有 IO 测速工具，坏道检测修复这些深度功能也没有，跟群晖的硬盘管理比差距明显」
> —— DX4600 一年使用体验（ifmo.cn）

**⑥ RAID 扩容逻辑反人类**

> 「换更大的硬盘不会扩容！四块 2T 组 RAID5，逐块换成 4T，全部换完容量还是原来的大小，白花钱。想扩容只能重建阵列」
> —— 同上

→ 系统底层逻辑，**App 救不了**，不在范围内。

**⑦ 备份与迁移苛刻**

> 「只支持单盘模式的数据迁移，RAID 模式下迁移功能不可用；也没有类似 Ghost 的整机打包备份」
> —— 同上

→ 且 UGOS **备份模块根本没有 API**，做不了。

**⑧ 播放器与音频体验（App Store 差评集中区）**

> 「播放视频没法选择音轨」
> 「影视中心手机播放视频，没办法选择内嵌的字幕」
> 「视频播放不能应用外小窗口」
> 「后台播放闪退」
> —— App Store 评论（2024-06 ~ 2025-09）

> 「动不动系统播放框就消失了，但后台有声音…app 网络经常在转发和 p2p 切换，打断后台播放！iOS 客户端后台播放的时候，人脸解锁进入 app 会打断播放 5s，必现」
> —— 社区 `thread-5624`，用户可见光

→ 播放器内核问题，自研成本极高，不在范围内（做播放器不如装 Infuse / Fileball）。

**⑨ 相册缺基础能力（官方论坛 9 条清单）**

备份按拍摄时间重命名 · 地理位置批量修改 · 照片重命名 · 标签 · 多维筛选（文件夹/相册/时间/标签/类型）· 释放本地已备份照片 · 删除 NAS 照片联动删本地 · 指定不备份的相册 · 上传时过滤已备份。
另：App Store 反馈「相册不知道什么忽然之间就停止自动备份」「想要去年今日」。

**⑩ DXP4800 Plus 专属：多风扇噪音**

> 「在 DXP4800 Plus 等高端型号上，由于散热逻辑与风扇调速算法的混乱，用户面临的是一种多频率噪音叠加的折磨…背部的大风扇与机箱内风扇在不同负载下的转速不匹配」
> —— TrueSight 审计报告（基于 Chiphell 社区数据分析）

→ App 无法改风扇曲线，但可以**把风扇转速实时化 + 异常告警**，让用户知道是哪把风扇在闹。

**⑪ 其他零散**
- 全局搜索缺排序、缺正则
- 手机端打不开 wps / et 格式表格
- 网盘缺夸克（官方回复「开发中」）
- 系统更新量被吐槽：「一个月就这，还不如飞牛一周的更新量」

### 3.3 一个值得记下的信号

2026-05-22 绿联上线**绿联云开发者平台**，面向第三方开放应用生态、可申请入驻 UGOS Pro 应用中心。说明官方在往开放走 —— 但 iOS 客户端这块短期内不会开放，自研仍然合理。

---

## 四、功能机会矩阵

### 4.1 官方 App 没有的（自研独有价值）

| 优先级 | 功能 | 依据 | 官方现状 |
|---|---|---|---|
| **T0** | 系统级推送（APNs）：盘温 / SMART / 坏道 / 存储池降级 / 容器异常退出 / UPS 切电池 | 用户评价①②最痛 | **完全没有**，只能进 App 主动刷 |
| **T0** | 手机端容器全操作：启停/重启/删除、实时日志搜索、CPU·内存曲线、环境变量、端口映射 | Docker API 完整 + 用户点名 | 只有「快捷访问」跳浏览器 |
| **T0** | 手机端编辑 docker-compose | 用户 UVRzwi 明确要求 | 没有 |
| **T0** | 镜像管理：搜索拉取 / 删除 / 清理悬空镜像 | Docker API 完整 | 没有 |
| T1 | 硬盘深度看板：全部盘位温度一屏 + SMART 属性明细 + 温度历史曲线 | 「硬盘管理偏弱」 | 只有单体基础 SMART |
| T1 | KVM 虚拟机 + **快照回滚** | KVM API 完整 | App 内基本缺失 |
| T1 | 文件系统快照查看 / 创建 / 回滚 | snapshot API 完整 | 只在 Web 端深处 |
| T1 | 风扇转速实时曲线 + 转速异常告警 | DXP4800 Plus 噪音吐槽 | 只给瞬时值 |
| T1 | 影视刮削手动纠错：搜豆瓣候选 → 单选替换 → 重刮 | 刮削源单一痛点 | 只有 TMDB，无法手动纠 |
| T2 | 固件更新提醒（只提醒，不动手升级） | firmware/upgrader | App 内可做但不便 |
| T2 | UPS 状态：电量 / 剩余续航 / 切换事件 | hardware/ups | 埋得很深 |
| T2 | 双 2.5G 网口协商速率与实时流量 | network/iface | 无图形化 |
| T2 | Widget / 灵动岛 / 快捷指令 | — | 没有 |
| T2 | 用户与权限列表 | user/ | 有但简陋 |

### 4.2 官方有但难用的（改良而非新增）

- 实时监控只有瞬时值 → 加历史曲线与温度趋势
- 容器日志不能搜索、不能跟随 → 支持关键词过滤与实时流
- 文件管理不能批量、无传输队列 → 加多选与后台队列
- 日志不能按模块+级别组合过滤 → 补齐
- 下载中心加磁力体验差 → 一键粘贴、进度推送

### 4.3 明确做不了 / 不该做

| 项 | 原因 |
|---|---|
| 备份 / 整机打包 | **UGOS 备份模块无 API**，物理上做不到 |
| RAID 扩容逻辑修正 | 系统底层阵列逻辑，App 无权干预 |
| 视频播放器（音轨/字幕/小窗/后台） | 播放器内核工程，成本 >> 收益，建议 Infuse / Fileball |
| 相册全部功能 | 官方在密集迭代 + photo v5 端点未文档化，追不上 |
| 坏道检测 / IO 测速 | 需底层 SMART 长测试与 fio，短线不做（V2 可考虑用测速容器绕） |
| 文件实时同步 | 官方桌面客户端独有，移动端无意义 |
| 系统升级执行 | 变砖风险，只做提醒 |

---

## 五、架构（一句话版本）

NAS 上跑一个**几十 MB 的 Docker 小程序**，替 iPhone 去跟 UGOS 的加密接口打交道，把能力整理成标准 HTTP + SSE；iPhone 用原生 SwiftUI 只连这个程序。

**为什么必须有这一层**：UGOS 要 RSA 加密密码、AES-GCM 包装请求体、双 token 轮换、证书 pinning。写进 iPhone 里，绿联一升级固件就得重新发版；放在 NAS 本地，固件变了只改它，App 一行不动。

1Panel / 宝塔是**可选的第二数据源**（绿联没有的运维能力才挂上去），不是主体。

---

## 六、MVP 范围（按痛点重排）

| 序 | 模块 | 说明 |
|---|---|---|
| 1 | **告警推送** | 中间层定时巡检 + APNs。盘温超阈、SMART 变化、存储池降级、容器退出、UPS 切电池 |
| 2 | **总览看板** | CPU/内存/网络 + 全部盘位温度 + 风扇转速 + 运行时长，带历史曲线 |
| 3 | **Docker 管理** | 容器全操作 + 日志搜索 + 镜像管理 + Compose 起停与编辑 |
| 4 | **文件管理** | 浏览 / 上传下载（后台队列）/ 重命名 / 删除进回收站 |
| 5 | **存储与硬盘** | 卷容量、SMART 明细、盘位温度 |
| 6 | **双地址接入** | 局域网 + 公网自动择优 |

V1：KVM 快照、文件系统快照、影视刮削纠错、Widget / 快捷指令、UPS、网口流量。

---

## 七、关键风险与应对

| 风险 | 应对 |
|---|---|
| 公网 IPv4 暴露 | 中间层强制 HTTPS；独立强 Token（非 NAS 密码）；登录失败限流封禁；可选 IP 白名单；**不暴露 UGOS 9443** |
| UGOS API 漂移 | 适配器隔离 + `/capabilities` 能力探测，接口失效时只让对应卡片显示「不支持」，不整页崩；记录已适配固件版本 |
| 无 Mac | GitHub Actions 云编译 + Development/Ad Hoc 描述文件（付费账号，1 年有效，支持 APNs 与 Widget） |
| App Store 审核 | 定位自用工具，走 Ad Hoc / TestFlight，不上架 |
| 中间层自己挂了 | 容器 restart policy + 启动自检 + 心跳（中间层挂了就没人发告警，需要外部兜底：可用第三方 uptime 监控盯中间层 `/health`） |

---

## 八、执行顺序

1. 中间层骨架：FastAPI + 配置 + Token 鉴权 + 统一数据模型 + `/health` + `/capabilities`
2. UGOS 适配器：登录 / 会话 / 自动重登 → 系统监控 → 存储 → Docker → 文件 → 日志
3. 巡检与推送：定时任务 + 阈值引擎 + APNs
4. Docker Compose 部署包 + NAS 上跑通验证
5. iOS：SwiftUI 骨架 + 设备管理 + 总览页 + 容器页
6. Actions 云编译 + 真机安装验证
7. 逐项补 V1
