# FastCF — Cloudflare IP 优选测速（Linux · 直连）

> **v5.1.0 性能与稳健性提升完成**：继续本任务前，先读取 `docs/DESIGN.md`（设计文档）
> 与 `docs/PROGRESS.md`（进度文档）。当前状态见进度文档"当前状态"一行。

> 基于 Python 3 + FastAPI 的 Cloudflare IP 优选工具：固定 **IPv4 · 443/TLS · 结果 5 个**，
> **指定 DC / 全局随机** 两种来源模式，**ICMP ping 预筛 + 并行下载测速（4 路并发 + 首包快速淘汰）**，
> 本地历史、手动 IP 池管理、CSV/JSON 导出、SSE 增量日志流。
> 无后台扫描线程——IP 池完全靠手动添加与扫描副产品积累。
> 所有测速流量**直连**（启动时自动清除 `http_proxy` / `https_proxy` / `all_proxy` 环境变量）。
> UI 为深石墨蓝控制台风格（OKLCH 设计令牌 + lime 青柠单一强调 + 明度分层 + 等宽数据字体）。

## 特性

- 🎯 **固定口径** — IPv4 · 443/TLS · 结果固定 5 个，不做协议/版本/数量选择
- 🌐 **双源合并随机** — 指定 DC（测该节点 IP 池）/ 全局随机（官方 + 外部清单合并采样，N 可调 10–2000）：
  - **官方段** [cloudflare.com/ips-v4](https://www.cloudflare.com/ips-v4)（14 条大段，/24 分层随机采样）
  - **外部清单** [zip.cm.edu.kg/all.txt](https://zip.cm.edu.kg/all.txt)（约 1.7 万条带国家标签的 IP:PORT，**仅取 443 端口**条目，直接随机）
  - 两源各占约一半名额、按 IP 去重；任一源失效时另一源自动补满
- 📡 **真·ICMP ping 预筛** — 系统 `ping` 命令（4 包、单包 2s 超时），**并发 200**；取平均时延 + 丢包率
- 🗄️ **DC 级 IP 池（纯手动 + 副产品）** — 每 DC 上限 50 个 IP，7 天 TTL；无后台线程，过期不删除，
  指定 DC 扫描时**事件性重验**（前台同步 ping 全池，失效剔除、存活刷新时间戳）
- 🔁 **回退补齐** — 指定 DC 池为空或达标不足 5 个 → 自动回退全局随机（按"随机 IP 数量"采样）把结果补齐到 5 个
- ✅ **入池校验** — 手动添加时：① 已知来源校验（官方 CF 段 ∪ 外部 443 清单，都不在拒绝）；② 并发拨号读 `cf-meta-colo` 按实际节点归池
- 📊 **两阶段测速** — 第一阶段 ICMP ping 并发预筛（丢包 ≥75% 淘汰并剔出池、时延 >2× 最佳淘汰、零丢包豁免）；
  第二阶段 **4 路并发** 443/TLS 下载测速（按延迟升序提交，**凑够 5 个达标即取消未开始的 future**），
  **多连接测速**（每个 IP 4 连接并发，绕过 GFW 单连接限速）+ **自适应提前结束**（快速成功 2s > 50Mbps / 快速失败 3s < 1Mbps / 首包快速淘汰 1.5s < 256KB）+ **失败重试**（0 Mbps 自动重试 1 次）
- 🔁 **未达标继续测** — 单个 IP 测速未达标（0Mbps/限流/低于下限）→ 继续测队列中下一个候选，不再单独换 IP 重试
- 🏆 **排名结果表** — rank 徽章（第 1 名强调色高亮）/ ping 延迟条形图 / 丢包率色阶 / 峰值速度色阶 /
  CF 数据中心中文名 / 归属地，按 **延迟 → 丢包 → 速度** 排序，支持按列排序
- 📜 **本地历史** — 自动保存最近 50 次，支持查看 / 复用参数 / 删除 / 下载 CSV
- 📤 **结果导出** — CSV（与 CFST result.csv 风格对齐）+ JSON
- 🎨 **深石墨蓝控制台 UI** — OKLCH 设计令牌 + lime 青柠单一强调（对比度 12.8:1，WCAG AA 远超）+
  明度分层可读性 + 等宽数据字体、双栏 Dashboard 布局、SSE 增量日志流（logDelta）、数据状态卡

## 快速开始

> 要求 Python 3.10+。依赖 `fastapi` + `uvicorn`（见 `requirements.txt`；
> 系统 site-packages 只读时：`pip install --target .vendor fastapi uvicorn`，
> 入口脚本会自动把 `.vendor` 加入 `sys.path`）。需要系统 `ping` 命令（Linux 默认自带）。

```bash
python3 fastcf.py                 # 启动并自动打开浏览器
python3 fastcf.py --port 8080     # 指定端口
python3 fastcf.py --no-browser    # 不自动打开浏览器
python3 fastcf.py --data-dir /x   # 指定数据缓存目录
```

### 入口参数

| 参数 | 说明 | 默认 |
|------|------|------|
| `--host` | 监听地址 | 127.0.0.1 |
| `--port` | 监听端口（0 = 自动分配） | 0 |
| `--no-browser` | 不自动打开浏览器 | 关 |
| `--data-dir` | 数据缓存目录（默认 `~/.fastcf`） | — |
| `--version` | 打印版本号 | — |

## 使用逻辑（完整流程）

用户只需填 5 个东西：**来源（指定 DC 或 全局随机）· 随机 IP 数量（随机时）· 测速时长 · 测速流量 · 速度下限**，点击开始。

```
┌─ 点击开始 ─────────────────────────────────────────────────────────┐
│                                                                    │
│  第一步（指定 DC 模式才走）                                         │
│   ① 取该 DC 的 IP 池                                               │
│      · 池为空 → 记日志，直接跳第二步（不自动建池）                    │
│      · 池已过期（>7天）→ 事件性重验：并发 200 ping 全池，             │
│        丢包 ≥75% 剔除、存活刷新时间戳                                │
│   ② ICMP ping 预筛（并发 200，每 IP 4 包）：                         │
│      丢包 ≥75% → 淘汰 + 从所属 DC 池剔除（locate 定位所属 DC）        │
│      时延 > 2× 最佳时延 → 淘汰（零丢包豁免）                          │
│   ③ 按 ping 升序串行下载测速（443/TLS，队列 = 全部预筛通过候选）：      │
│      达标（≥速度下限；下限=0 时 >0 即达标）凑够 5 个 → 停止            │
│      未达标 → 继续测队列中下一个候选                                   │
│   ④ 队列耗尽仍未够 5 → 记日志，进入第二步回退补齐                      │
│                                                                    │
│  第二步（全局随机模式 / 回退补齐）                                    │
│   ① 双源合并采样「随机 IP 数量」个：                                  │
│      外部 443 清单（zip.cm.edu.kg/all.txt）约一半 + 官方 ips-v4       │
│      段（/24 分层随机）补满，按 IP 去重                               │
│   ② ICMP ping 预筛（同上，并发 200）                                │
│   ③ 按 ping 升序串行下载测速（队列 = 全部预筛通过候选）：             │
│      · 每个 IP 测速前探测 cf-meta-colo → 确认实际 DC 并入池           │
│      · 达标凑够所需数量 → 停止；未达标 → 继续测下一个                 │
│                                                                    │
│  汇总                                                              │
│   · 测速成功（>0Mbps）的 IP 回写其实际 DC 池                          │
│   · 合并两步结果 → 按 延迟 → 丢包 → 速度 排序 → 取前 5 个输出          │
└────────────────────────────────────────────────────────────────────┘
```

**关键语义**：
- 「速度下限」= 0（默认）：任何速度 >0 都算达标，凑够 5 个即停；>0：按下限判定。
  **下载队列 = ping 预筛通过的全部候选（延迟升序）**——单个 IP 未达标就继续测下一个，
  直到凑够 5 个达标或队列耗尽（仍不足则返回实际数量，指定 DC 模式再回退随机补齐）
- 回退触发：指定 DC 池为空、或指定 DC 达标数 < 5。随机模式本身不触发回退
- ping 是**系统 `ping` 命令的 ICMP 时延**，与 443/TLS 下载测速是两个独立阶段
- 外部清单条目**只用 IP、端口标签忽略**（固定 443 口径）；清单中非 CF 节点会被
  ping 预筛与 TLS 实测自然淘汰，无需白名单

## IP 池

无后台线程。池的 IP 来自四个途径：

| 途径 | 说明 |
|------|------|
| 手动探测并添加 | 前端"IP 池管理"面板：已知来源校验（官方段 ∪ 外部 443 清单）→ 并发拨号读 `cf-meta-colo` → 按实际 DC 归池 |
| 随机 IP 测速前探测 | 随机模式进入下载测速的 IP，先探测实际服务节点并入池 |
| 测速成功回写 | 任一模式中测速 >0Mbps 的 IP 回写其实际 DC |

池规则：每 DC 上限 **50** 个 IP（超出保留最新）；**7 天 TTL**——过期不删除、不后台重探，
只在**该 DC 池被指定 DC 扫描用到时**触发事件性重验（前台同步，ping 全池，
丢包 ≥75% 剔除、存活刷新时间戳）。ping 丢包 ≥75% 的 IP 在任何扫描中都会立即从所属 DC 池剔除。

## 项目结构

```
fastcf.py              # 入口脚本（代理清除 + .vendor 引导 + web 服务启动 + 后台预热双源缓存）
fastcf/
  __init__.py          # 包初始化（版本号单一来源）
  config.py            # 常量 + ScanParams（Pydantic 参数校验）
  net.py               # 直连下载（总时间预算）/ 原子写 / 重试
  store.py             # 统一持久化（单锁 + 原子写 + 7 天缓存）
  zhnames.py           # 静态中文映射（国家 149 / 城市 178，从 colos.py 拆出）
  colos.py             # CF colo 参考表（静态快照 + 在线刷新 3 天 TTL + 国家分组）
  sources.py           # 双源获取（官方 ips-v4 + 外部 443 清单）/ 缓存 / 合并采样 / 已知来源校验
  pool.py              # DC 级 IP 池（管理 / 入池校验 / 事件性过期判定）
  history.py           # 历史记录（基于 store）
  scanner.py           # 测速引擎（EngineContext 依赖注入；ICMP ping 并发预筛 + 443/TLS 下载测速 + 回退编排）
  appstate.py          # 进程级状态机（idle/running/done/error/cancelled；SSE 发布订阅）
  server.py            # FastAPI 路由（静态 UI + JSON API + SSE 流）
  exports.py           # 结果导出（csv / json）
  web/
    index.html         # UI 页面（双栏 Dashboard）
    style.css          # 样式（浅色控制台 · Linear 式安静高级）
    app.js             # 前端逻辑（SSE、导出、历史、IP 池、数据状态）
tests/
  test_units.py        # 离线单元测试（21 个；mock EngineContext，不触网）
docs/
  DESIGN.md            # 重构设计文档（架构 / 状态机 / 持久化 / API / UI 方向）
  PROGRESS.md          # 重构进度文档
LICENSE                # MIT
requirements.txt       # 依赖说明（fastapi + uvicorn）
README.md              # 本文档
```

**依赖方向**（无环）：`config` ← `net` ← `store` ← `sources` / `pool` / `history` / `colos`；
`scanner` 通过 `EngineContext` 注入 `sources` / `pool` / `colos` / `net`（可离线 mock）；
`appstate` 持有 `scanner`；`server` 持有 `appstate`。

## 数据源

| 数据 | 来源 | 更新策略 |
|------|------|----------|
| CF IPv4 段 | `https://www.cloudflare.com/ips-v4`（官方 14 条大段） | 7 天缓存，过期自动刷新；下载失败沿用旧缓存 |
| 外部 IP 清单 | `https://zip.cm.edu.kg/all.txt`（`IP:PORT#国家` 格式，仅保留 443 端口、去重后约 1 万+ 条） | 7 天缓存，过期自动刷新；失效时随机来源退化为仅官方段 |
| CF colo 参考表 | [Netrvin/cloudflare-colo-list](https://github.com/Netrvin/cloudflare-colo-list) `DC-Colos.json`（内置静态快照兜底） | 3 天 TTL，在线失败沿用快照 |
| 测速节点 | `speed.cloudflare.com/__down?bytes=N`（443/TLS） | 实时请求（`cf-ray` / `cf-meta-*` 头返回实际服务地） |

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | Web UI |
| GET | `/api/status` | 当前扫描状态 + 最近一次结果 |
| GET | `/api/history` | 历史记录列表 |
| GET | `/api/stream` | SSE 实时日志流 |
| GET | `/api/colos` | 全部 CF 节点列表（含各 DC 池大小），按国家分组（中国系置顶） |
| GET | `/api/pools` | IP 池明细（节点 / 国家 / 数量 / IP 列表 / 是否过期） |
| GET | `/api/data-status` | 数据目录 / 双源缓存（段数 · 清单条数 · 时间戳）/ 池统计 / 版本 |
| GET | `/api/export?fmt=…` | 下载结果（`fmt` ∈ csv/json；`source` ∈ latest/history） |
| POST | `/api/scan` | 开始扫描（body 见下） |
| POST | `/api/cancel` | 取消当前扫描（前端点「取消」时先弹确认框；取消后界面自动恢复可扫描，已测出的部分结果保留在日志中） |
| POST | `/api/history` | 历史操作（`{action: "delete"|"clear", id}`） |
| POST | `/api/pools` | 池操作（`{action: "add"|"clear"|"clear_all"|"remove_ip"}`；`add` 时 `code` 可省略，IP 按实际探测的 colo 归池，并先做已知来源校验（官方段 ∪ 外部 443 清单）；`remove_ip` 删除指定 DC 的单个 IP） |

### 扫描参数（POST /api/scan body）

```json
{
  "mode": "DC",
  "colo": "HKG",
  "randomCount": 150,
  "speedSecs": 8,
  "speedMB": 50,
  "minSpeed": 0
}
```

- `mode`：`DC`（指定节点）或 `RANDOM`（全局随机）
- `colo`：`mode=DC` 时的节点三字码（如 HKG）
- `randomCount`：随机模式采样的 IP 数量（10–2000，默认 150）；DC 模式不足时回退随机也用它
- `speedSecs`：每个候选 IP 的测速时长（3–60s，默认 8）
- `speedMB`：下载流量上限（10–1000MB，默认 50），速度取时长内峰值
- `minSpeed`：速度下限（Mbps）。0 = 任何速度 >0 都算达标；>0 时按下限判定。达标凑够 5 个即停

### 时延口径

- **排名用的时延 = ICMP ping**（系统 `ping` 命令，4 包取平均，中文/英文输出均解析
  `rtt min/avg/max/mdev` 行；注意 Linux iputils `ping -W` 单位为**秒**）
- 下载测速的 TCP+TLS 连接时延仅记录在结果里供参考，**不参与排名**
- 丢包率 = ping 失败包数 / 总包数；≥75% 淘汰并从所属 DC 池剔除

## 配置

- 数据缓存目录：`~/.fastcf/`（可通过 `FASTCF_HOME` 环境变量或 `--data-dir` 覆盖）
- 官方段缓存：`~/.fastcf/cf_ips.json`（14 条 CIDR）；外部清单缓存：`~/.fastcf/ext_ips.json`
  （443 端口 IPv4 列表）。均缓存 7 天，过期自动刷新，下载走直连（绕系统代理），
  偶发中断自动重试 3 次（退避 1s/2s）；落盘为**原子写**（临时文件 + rename）
- colo 参考表：`~/.fastcf/colo_data.json`
- 历史记录：`~/.fastcf/history.json`
- IP 池：`~/.fastcf/ip_pools.json`
- colo 参考表下载加速源（可选）：`FASTCF_PROXY_BASE` 环境变量，前缀形式（如 `https://your-proxy/`），留空则用 jsDelivr / GitHub raw 直连

## 开发

- 测试：`python3 tests/test_units.py`（离线单元测试，21 个；用 `FASTCF_HOME` 临时目录隔离数据，
  `EngineContext` mock 不触网）
- 实现约定：CIDR 切分（`v4_prefixes`）一律走 `ipaddress` 标准库的字符串 API
  （`ip_network(str, strict=False)`、`.subnets(new_prefix=)`、`.hosts()`），
  **不做手写的整数位运算**（`<<`、`>>`、`&`、`~`）。部分 Python 构建下大整数位运算结果不可靠，
  交给标准库逐 IP 枚举更安全
- ICMP ping 走 `subprocess` 调系统 `ping`（`-c 4 -W 2`，-W 单位为秒），不手写 raw socket
- 版本号单一来源：`fastcf/__init__.py` 的 `__version__`
- 前端：原生 HTML/CSS/JS，零构建零 CDN；UI 改动后无需重新编译（服务端启动时读入内存，
  改 UI 文件需重启服务生效）
- 状态管理：`AppState` 为唯一进程级状态源；`store.py` 统一持久化（原子写 + 单锁）；
  前端只保留表单草稿状态，运行态/结果/历史/池全部来自后端

## 许可证

- 本项目代码：MIT（见 [LICENSE](LICENSE)）
- colo 参考数据快照：来自 [Netrvin/cloudflare-colo-list](https://github.com/Netrvin/cloudflare-colo-list)（MIT）
- 外部 IP 清单：来自 [zip.cm.edu.kg](https://zip.cm.edu.kg/all.txt)（仅使用其中的 IP 地址）
