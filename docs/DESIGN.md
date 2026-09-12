# FastCF 重构设计文档

> 继续本任务前，先读取本文档与 `docs/PROGRESS.md`。

## 1. 目标与核心需求（保持不变）

寻找**当前网络环境下最优的 Cloudflare IPv4 IP**，固定口径：

- IPv4 · 443/TLS · 结果固定 5 个
- 两种来源模式：指定 DC（测该节点 IP 池）/ 全局随机（官方段 + 外部清单合并采样，N 可调 10–2000）
- 两阶段测速：ICMP ping 并发预筛（系统 `ping` 命令）→ 按延迟升序串行 443/TLS 下载测速
- 指定 DC 池为空或达标不足 5 个 → 自动回退全局随机补齐
- 本地历史（最近 50 次）、手动 IP 池管理、CSV/JSON 导出、SSE 实时日志
- 所有测速流量直连（启动时清除代理环境变量）

## 2. 重构动机（现有代码问题）

| 问题 | 位置 | 重构对策 |
|------|------|----------|
| 模块职责混杂：`colos.py` 同时承担 150+ 行国家/城市中文映射、在线刷新、分组 | `colos.py` | 拆为 `colos.py`（参考表 + 刷新）与 `zhnames.py`（静态映射） |
| 状态散落：`pool.py` 用 4 个模块级全局变量 + 惰性加载；`appstate.py` 与 `scanner.py` 各自维护日志/进度 | `pool.py` `appstate.py` | 引入 `store.py` 统一持久化（原子写 + 单锁）；`AppState` 成为唯一状态聚合点 |
| 扫描编排与 IO 耦合：`Scanner` 直接 import 具体模块，无法注入 mock | `scanner.py` | `Scanner` 通过 `EngineContext` 注入依赖（sources/pool/probe 可替换），单测可离线跑完整流程 |
| SSE 订阅者管理在 Scanner 内部，队列满即踢人，前端重连丢状态 | `scanner.py` | 保留 pub/sub 但增加"迟到订阅者直接拿 last_state"语义（已有），并让 done 后 SSE 立即回最终态（已有，保留） |
| 前端状态与后端状态双份维护（`state` 对象 + 轮询） | `app.js` | 前端只保留"表单草稿"状态；运行态/结果/历史/池全部来自后端，SSE 为唯一实时通道 |
| UI 深色风格与 README 描述（浅色 Linear 式）不一致；部分交互（弹窗 hidden 属性被 CSS 覆盖）曾出 bug | `style.css` | 重写为浅色 Linear 式：扁平面板 + 发丝边框 + 单一青色强调 + 等宽数据字体；`[hidden]` 用 `display:none !important` 保证 |
| `requirements.txt` 写"零依赖"但实际依赖 fastapi/uvicorn（.vendor 兜底） | `requirements.txt` | 如实声明：fastapi + uvicorn（系统 site-packages 只读时 `pip install --target .vendor`） |

## 3. 架构

```
fastcf.py                  # 入口：代理清除 + 端口分配 + uvicorn 启动
fastcf/
  __init__.py              # 版本号单一来源
  config.py                # 常量 + ScanParams（Pydantic 校验）
  zhnames.py               # 静态中文映射（国家/城市）
  data_colos.py            # colo 参考表静态快照（Netrvin 快照，离线兜底）
  colos.py                 # Colos 参考表：快照 + 在线刷新（3 天 TTL）+ 国家分组
  net.py                   # 直连下载（绕代理 + 重试退避）/ 原子写
  store.py                 # 统一持久化：pools / history / 源缓存（原子写 + 单锁）
  sources.py               # 双源获取（官方段 + 外部 443 清单）/ 缓存 / 合并采样 / 已知来源校验
  pool.py                  # DC 级 IP 池（基于 store；TTL 语义不变）
  history.py               # 扫描历史（基于 store）
  ping.py                  # 系统 ping 封装（4 包精确 + 1 包探测）
  speedtest.py             # 443/TLS 下载测速 + cf-meta-colo 节点探测
  scanner.py               # 扫描编排器（状态机 + 事件流；依赖经 EngineContext 注入）
  appstate.py              # AppState：进程级单例，唯一状态源（扫描状态机 + 最近结果 + 系统概要）
  exports.py               # CSV / JSON 导出
  server.py                # FastAPI 路由 + SSE
  web/
    index.html / style.css / app.js   # 浅色 Linear 式 UI
tests/
  test_units.py            # 离线单元测试（不触网；FASTCF_HOME 临时目录隔离）
```

### 3.1 依赖方向

```
server → appstate → scanner → {sources, pool, colos, ping, speedtest}
pool / history / sources → store → net
```

无环。`scanner` 不 import `server`/`appstate`（由 appstate 持有 scanner 实例）。

## 4. 状态管理（单一事实来源）

### 4.1 进程内（AppState，单例）

| 状态 | 类型 | 说明 |
|------|------|------|
| `scanner` | `Scanner \| None` | 当前扫描编排器；`scanner.done` 事件是"是否运行中"的唯一判据 |
| `last_result` / `last_params` | dict | 最近一次成功结果 + 参数 |
| 锁 | `threading.Lock` | 保护 scanner 切换与 last_result 写入 |

扫描状态机：`idle → running → done / error / cancelled`。
所有状态变更经 `Scanner._emit` 推送给 SSE 订阅者（200ms/2% 节流）。

### 4.2 持久化（store.py，全部原子写）

| 文件 | 内容 | TTL |
|------|------|-----|
| `~/.fastcf/cf_ips.json` | 官方 CF IPv4 段（14 条 CIDR） | 7 天，过期自动刷新，失败沿用旧缓存 |
| `~/.fastcf/ext_ips.json` | 外部 443 清单（去重 IPv4） | 7 天，同上 |
| `~/.fastcf/colo_data.json` | colo 参考表在线刷新结果 | 3 天，失败沿用内置快照 |
| `~/.fastcf/ip_pools.json` | DC 级 IP 池 `{DC: {ips, ts}}` | 池按最后入池时间 7 天过期（不删除，事件性重验） |
| `~/.fastcf/history.json` | 最近 50 次扫描记录 | 滚动保留 |

### 4.3 前端

- 表单草稿（mode/colo/各滑杆值）只存前端，提交时打包成 scan 参数
- 运行态、进度、日志、结果、历史、池、数据状态全部来自后端（SSE + REST）
- SSE 断线自动重连（EventSource 原生）；重连后若扫描已结束，服务端直接回最终态

## 5. 扫描流程（语义与原版一致）

```
A. 候选集
   DC 模式：取该 DC 池（空 → 直接回退随机；过期 → 事件性重验：并发 ping 全池，
            丢包 ≥75% 剔除、存活刷新时间戳）
   随机模式：双源合并采样 randomCount 个（外部清单约一半 + 官方段 /24 分层随机，去重）
B. ping 预筛（并发 200）
   阶段 1：1 包探测，快速淘汰不可达
   阶段 2：4 包精确测量（仅存活者）
   淘汰规则：丢包 ≥75%（并从所属 DC 池剔除）；时延 > 2× 最佳（零丢包豁免）
C. 下载测速（443/TLS，按延迟升序串行）
   队列 = 全部预筛通过候选；随机 IP 测速前探测 cf-meta-colo 确认实际 DC 并入池
   达标（≥minSpeed；minSpeed=0 时 >0 即达标）凑够 5 个 → 停止
   未达标 → 继续测队列中下一个候选
D. 回退：DC 模式不足 5 个 → 随机模式再跑 B+C 补齐
E. 汇总：测速成功（>0Mbps）的 IP 回写其实际 DC 池；
   按 延迟 → 丢包 → 速度 排序取前 5
取消：保留已测出的部分结果，标记 cancelled
```

## 6. API 约定（与原版兼容）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` `/app.js` `/style.css` | Web UI（启动时读入内存） |
| GET | `/api/status` | 当前扫描状态 + 最近一次结果 |
| GET | `/api/history` | 历史记录列表 |
| POST | `/api/history` | `{action: delete/clear, id}` |
| GET | `/api/colos` | 全部 CF 节点（国家分组，中国系置顶，含各 DC 池大小） |
| GET | `/api/pools` | IP 池明细 |
| POST | `/api/pools` | `{action: add/clear/clear_all/remove_ip}` |
| GET | `/api/data-status` | 数据目录 / 双源缓存 / 池统计 / 版本 |
| GET | `/api/export` | `fmt` ∈ csv/json；`source` ∈ latest/history |
| POST | `/api/scan` | 开始扫描（非法参数 422；运行中 409） |
| POST | `/api/cancel` | 取消当前扫描 |
| GET | `/api/stream` | SSE 实时状态流 |

扫描参数：`mode`(DC/RANDOM) · `colo` · `randomCount`(10–2000) · `speedSecs`(3–60) · `speedMB`(10–1000) · `minSpeed`(0–10000)。

## 7. UI 设计方向（浅色 Linear 式）

- 浅色控制台：`#f7f8fa` 底 + 白色面板 + 发丝边框（1px `#e5e7eb` 系）+ 单一青色强调（`#0e7490`/`#22d3ee` 系）
- 等宽数据字体（JetBrains Mono 栈）用于 IP/延迟/速度等数值
- 布局：顶部 appbar（品牌 + 运行指示 + 池/信息入口）→ KPI 数据状态条 → 双栏（左：扫描设置；右：进度/日志 + 结果/历史 tabs）
- 交互：SSE 实时日志流、进度条、结果表按列排序、历史复用参数、IP 池管理弹窗、系统信息弹窗、toast 反馈
- 动效克制：入场 fadeUp、状态点 pulse；无重阴影、无渐变滥用
- `[hidden]` 全局 `display:none !important`，避免弹窗状态 bug 复发

## 8. 测试策略

- `tests/test_units.py`：离线、零网络（`FASTCF_HOME` 指向临时目录）
- 覆盖：CIDR/外部清单解析、采样降级、已知来源校验、池 CRUD/上限/过期/索引、
  参数校验、导出、历史、colo 快照、ping 输出解析、扫描器 finalize/取消/错误、
  AppState 完整生命周期（离线快速结束）
- 运行：`python3 tests/test_units.py`
