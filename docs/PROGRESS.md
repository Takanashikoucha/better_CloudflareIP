# FastCF 重构进度

> 继续本任务前，先读取 `docs/DESIGN.md` 与本文档。

## 当前状态

**已完成**：v6.0.0 彻底重构（UI 简化 + 浅色极简设计 + Cloudflare 限速保护强化，参考 6ird/tools/cfip 的 IP 源与测速口径）。

**v6.0.0 变更**：

1. **前端彻底重构（web/ 三件套）**：
   - 布局简化：4 张 KPI 卡 → 单行数据状态条（数据源/池/节点/版本 + 源健康标记 fresh/stale/missing）
   - 控件简化：5 个滑杆 → 2 个预设按钮（快速 5s/5MB/下限0 · 精准 15s/10MB/下限50）+ 高级选项折叠
   - 结果表 7 列 → 5 列（#/IP/节点/延迟/速度）；历史/IP 池/系统信息收进右侧抽屉（Esc 关闭）
   - 新增「直连」状态胶囊（明示测速流量不走代理）+ 429 限速提示条（SSE throttled 透出）
   - 配色（color-expert 方法论）：浅色极简 Linear 式；OKLCH 令牌 + teal 单一强调
     （oklch(55% 0.13 185)，与状态色 绿/琥珀/红 色相分离）+ color-mix(in oklab) 派生
     + 明度分层可读性 + 60-30-10 + 形状一致性锁定 + prefers-reduced-motion 支持
   - 修掉旧版深色主题与 README 描述的长期不一致

2. **Cloudflare 限速保护强化（speedtest.py / scanner.py / config.py）**：
   - 单 IP 流量预算：min(设定流量, 20MB)（SPEED_BUDGET_MB）
   - 相邻 IP 测速间隙 0.5s（SPEED_GAP），压低持续速率
   - 429 双层退避：原有 Retry-After 短冷却（≤30s）+ 新增全局冷静期
     （10s 起步、每遇一次 429 翻倍、≤60s、成功 IP 递减复位），避免 4 并发 IP 轮番撞限速
   - 429 状态透出：scanner.throttled → SSE → 前端红色提示条 + 日志警告
   - 前端测速流量滑杆上限 1000MB → 20MB（与预算对齐）

3. **后端小幅增强**：
   - `sources.sources_status()` 增加 `health` 字段（fresh/stale/missing）
   - `/api/data-status` 增加 `src_health_official` / `src_health_external`
   - 版本号 5.1.0 → 6.0.0

**参考站调研结论（6ird/tools/cfip）**：
- IP 源 = 官方 cloudflare.com/ips-v4 CIDR × 每段 N 样本（无外部清单）
- 测速 = TCP 连接延迟(ms) + TLS 下载速度(Mbps)，支持自定义测试 URL，支持 v4/v6
- 采纳其「简洁流程 + 延迟/速度双指标」思路；保留本项目更强的双源合并 + ICMP 预筛 + 节点池能力
- 去除其广告弹窗等噪音设计

**端到端验证**：
- 单元测试：29 个全部通过（`python3 tests/test_units.py`）
- 服务启动冒烟：首页 / API 端点 / 新版本号 正常
- 浏览器验收：桌面 + 移动视口截图（webapp-testing）

## v5.1.0 变更（保留）

**v5.1.0 性能与稳健性提升**（测速 0 Mbps 根因修复 + 429 限速处理 + 取消/状态显示一致性 + 锁竞争修复）。

**v5.1.0 变更**：

1. **speedtest.py 重写**：
   - 429 检测修正：`b" 429 " in head.split(b"\r\n", 1)[0]`（原 `b"429" in head` 会匹配 content-length 中的 "429"）
   - 429 退避：进程级 `_throttle_wait`/`_throttle_set`（上限 30s，可被取消打断）
   - 自适应提前结束：快速成功（2s > 50Mbps）/ 快速失败（3s < 1Mbps）/ 首包快速淘汰（1.5s < 256KB）
   - 取消感知：`is_cancelled` 每轮检查，recv 超时 2s 内响应
   - 失败重试：0 Mbps → 自动重试 1 次（间隔 2s），取两次中较好的结果
   - `speedMB` 默认 50 → 5（CF 限速阈值 ~5MB）；最小值 10 → 1

2. **scanner.py 修正**：
   - 快速失败：连续 3 个 IP 都 0Mbps → 提前停止（达标即清零计数）
   - 取消检查：`_measure` 入口检查 `_cancelled()`
   - 探测入池去重：`if ip not in pool` → "已入池" / "（池已有）"
   - 进度映射：rtt 20-40，speed 45-90（90-100 留给汇总）
   - `run()` 尾部：取消 → `_finalize(cancelled=True)`；空结果 → `_finish_error`；否则 → `_finalize`

3. **状态显示一致性**：
   - `appstate.status()` 返回 `stage` 字段（done / cancelled / error）
   - `server.py` SSE：`cancelled` 阶段也结束流（原只处理 done/error）
   - `app.js`：`setRunning` 处理 cancelled/error 状态；`stageName` 映射补全
   - `style.css`：`.run-ind.cancelled` / `.run-ind.error` 样式

4. **稳健性修复**：
   - `pool.py`：所有读写操作在 `_lock` 内（原 `touch()`/`expired()` 在锁外调 `_ensure_loaded()`）
   - `store.py`：`write_json` 只读文件系统容错（静默失败，不中断扫描）
   - `net.py`：`direct_download` 预算检查移到循环开头（原在循环末尾，可能多等一次 sleep）
   - `appstate.py`：`cancel()` 先取引用再释放锁（与 `start()` 锁序一致）
   - `server.py`：启动预热异常兜底（失败不影响服务启动）

5. **前端**：
   - `index.html`：`inMB` 滑块 min 10→1，step 10→1，默认 50→5
   - `app.js`：`bindRange` min 10→1；SSE 处理 cancelled 阶段
   - `style.css`：cancelled/error 指示器样式

**端到端验证**：
- 服务启动 / 全部 API 端点（status / data-status / pools / history / colos）
- 真实扫描（RANDOM 100 IP → 5 个达标结果，28s）：
  - 45.142.166.211 (NRT) 33 Mbps
  - 50.7.21.117 (SIN) 1 Mbps
  - 45.150.128.159 (BKK) 33 Mbps
  - 162.159.228.146 (LAX) 10 Mbps
  - 104.17.134.52 (SJC) 20 Mbps
- 单元测试：29 个全部通过（`python3 tests/test_units.py`）

**GFW 环境说明**：
- 本环境在中国大陆，CF 被 GFW 严重干扰/限速
- 单连接：0-2 Mbps；4 连接：0-39 Mbps（波动大）
- 约 50% 随机官方段 IP 握手超时（GFW 干扰）
- 可用 IP 给 1-33 Mbps（4 连接）
- 0 Mbps 结果部分是环境因素；代码修复使超时/429 被优雅处理而非静默产生 0

## v5.0.2 变更（保留）

- **EOF 不重开连接**：`/__down?bytes=N` 只服务 N 字节就 EOF；原代码 EOF 直接 `break`，4 连接在 0.6-1.1s 全部 EOF，实际下载 ~0 字节 → 0 Mbps。修复：EOF 后不重开连接，用实际下载时间计算速度
- **速度计算修正**：速度 = 实际下载大小 / 实际下载时间（不是滑动窗口峰值）
- **429 限速处理**：`speedMB` 默认 50 → 5（CF 限速阈值 ~5MB）；`_open_conn` 收到 429 时自动降级到 5MB 重试
- **scanner.py 修正**：`speed_mb` 最小值 10 → 5（与 config.py 一致）

## v5.0.1 变更（保留）

- **多连接测速**：单连接 → 4 连接并发（`SPEED_CONNS=4`），绕过 GFW 单连接限速
- **自适应提前结束**：快速成功（2s > 50Mbps）/ 快速失败（3s < 1Mbps）/ 首包快速淘汰（1.5s < 256KB）
- **失败重试**：0 Mbps → 自动重试 1 次（间隔 2s），取两次中较好的结果
- **快速失败**：前 3 个 IP 都 0Mbps → 提前停止（网络异常）
- **GFW 提示**：前端检测速度 < 10 Mbps → 显示"可能受 GFW 影响" + 速度预期

## v5.0.0 变更（保留）

- **并行下载测速**：串行 → 4 路并发（`SPEED_WORKERS=4`），按延迟升序提交，凑够达标数即取消未开始的 future
- **SSE 增量日志**：帧带 `logDelta`（新增日志）+ `logTotal`；迟到订阅者首帧全量 last_state（前端重置本地日志数组）
- **last_error**：扫描错误在 scanner 被替换后仍可见（`status().error`）
- **rank 字段**：结果按 延迟→丢包→速度 排序名次（1-5），前端 rank 徽章（第 1 名强调色高亮）
- **UI 全新设计语言**：深石墨蓝底 + lime 青柠单一强调（OKLCH 令牌，对比度 12.8:1）+ 明度分层 + `color-mix(in oklab)` 派生 hover/soft 态
- **遗留清理**：删除 `pools.py` / `geoip.py` / `ipdata.py`（v4 零引用）

## 已完成

- [x] 梳理现有代码，识别 7 类重构问题（见 DESIGN.md §2）
- [x] 设计文档 `docs/DESIGN.md`（架构 / 状态机 / 持久化 / API / UI 方向）
- [x] 进度文档 `docs/PROGRESS.md`
- [x] 后端核心模块重构（store / zhnames / colos / sources / pool / history / scanner / appstate / net）
- [x] API 层重构：`server.py`（FastAPI 路由 + SSE，与原版兼容）
- [x] 前端 UI 重构：浅色 Linear 式（`index.html` / `style.css` / `app.js`）
- [x] 单元测试：29 个全部通过（`python3 tests/test_units.py`）
- [x] 端到端验证：服务启动 / API / 真实扫描（RANDOM 100 IP → 5 结果，28s）/ DC 模式 / 参数校验 / 取消 / 历史 / 池 / 导出
- [x] 界面审美升级（v4.1.0）：深色精密控制台风格

## 阻塞点

无。

## 关键决策记录

- 技术栈保持 Python + FastAPI（.vendor 兜底方案保留）
- 状态管理：`AppState` 为唯一进程级状态源；`store.py` 统一持久化（原子写）
- 扫描语义与原版完全一致（两阶段测速 / 回退 / 池规则 / 取消保留部分结果）
- UI 改为浅色 Linear 式（与 README 描述一致），`[hidden]` 用 `!important` 防弹窗 bug 复发
- `scanner` 通过 `EngineContext` 注入依赖，单测可离线跑完整流程
- `direct_download` 加总时间预算（`timeout × retries + 5` 秒），防慢握手拖死调用方
- 429 退避：进程级冷却（上限 30s），避免 Retry-After 异常值拖死扫描
- 只读文件系统容错：`store.write_json` 静默失败，不中断扫描（沙盒环境）

## 网络环境说明（测试时）

本环境到 Cloudflare 的 TCP/TLS 握手较慢（~17-20s），导致：
- 单元测试 `test_appstate` / `test_sample_fallback` 耗时 ~2-3 分钟（真实网络 ping 预筛）
- 真实扫描 100 个 IP 耗时 ~28s（ping 预筛 ~7s + 下载测速 ~21s）
- 功能全部正常，只是慢；生产环境网络正常时速度会快很多
