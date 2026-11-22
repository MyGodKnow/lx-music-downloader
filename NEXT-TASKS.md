# 下一步任务（面向接手者）

> 更新时间：2026-11-22
> 仓库：`C:\Users\God Know\Documents\code\lx-music-download`
> 远端：https://github.com/MyGodKnow/lx-music-downloader （分支 `main`）
> 当前版本：**1.1.1（已发布）** — https://github.com/MyGodKnow/lx-music-downloader/releases/tag/v1.1.1
> 本文档替代 `HANDOVER.md` 中已过期的待办段落；历史背景仍可查阅 HANDOVER。

---

## 一、先读这些，别重复踩坑

### 环境事实（都是实测得出，不是猜测）

| 事项 | 结论 |
|---|---|
| **网络** | `git push` / GitHub API 必须走代理：`HTTPS_PROXY=http://127.0.0.1:7896`（Clash 用 **7896**，不是常见的 7890）。不设就报 `Connection was reset`。 |
| **`gh` CLI** | **未安装**。建 Release / 传资产用 REST API。凭据在 Windows 凭据管理器 `git:https://github.com`（用户 `MyGodKnow`，有 admin 权限）。 |
| **git 凭据助手** | `~/.gitconfig` 指向 `C:\Users\God Know\.workbuddy\binaries\PortableGit\...\git-credential-wincred.exe`，**该文件不存在**。push 仍能成功（凭据已缓存），但 `git credential fill` 会卡住等输入——**不要用**，会挂死。 |
| **引擎测试** | 必须用 **Python 3.13**（带 mutagen）：`C:\Users\God Know\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe`。DSH 自带 Python 3.12 **没有 mutagen**，跑测试会有 6 项假失败（`detect_actual_quality` 返回 `''`）。用 3.13 跑 → **184 项全通过**。 |
| **发布构建** | 必须 `cd lx-tauri && npx tauri build --no-bundle`。**`cargo build --release` 不含前端资源**，产物会显示「localhost 拒绝连接」。两者输出路径相同，极易搞混。 |
| **engine.zip** | 已 gitignore。只在改 Python/Node 时重打（PyInstaller + Python 3.13）。当前引擎包（2026-11-22 09:16）已含 `_ensure_utf8_stdio` 修复，**不要无谓重打**。 |

### 上一轮做了什么（1.1.1）

修复「双击后窗口闪一下就消失」。根因：WebView2 的 profile 目录建不出来 →
`CreateCoreWebView2Controller` 失败 → wry 销毁窗口 → Tauri 事件循环不退出 →
进程变成「任务管理器里看得见、界面上没有」的僵尸。

落地了四层防护（都在 `lx-tauri/src-tauri/src/lib.rs`）：

| 机制 | 位置 |
|---|---|
| 数据目录四级回退（实测可写性） | `data_root()` @ 69 |
| WebView2 数据目录可写性探测 | `webview2_data_dir()` @ 2320 |
| 建窗失败按错误类型自愈重试 | `create_main_window()` @ 2243、`is_webview_recoverable()` @ 2302、`reset_webview_data_dir()` @ 2335 |
| 启动看门狗 | `spawn_startup_watchdog()` @ 2198 |
| 启动日志 / 错误弹框 | `startup_log()` @ 117、`run()` @ 2103、`record_boot_trace()` @ 2060 |

---

## 二、P0：三件事（✅ 已于 2026-11-22 全部完成并验证）

> 原则：**先证明问题存在，再改代码**。上一轮的教训是「加了防护但没验证防护本身是否有效」。
>
> 三项目前状态：P0-1 / P0-2 / P0-3 均已落地，验证记录见每节末尾的「验证结果」。
> 版本号尚未改动（仍为 1.1.1），发版由作者决定。

### P0-1　看门狗判据可能完全无效（最优先）

**现象／证据**

`spawn_startup_watchdog()`（`lib.rs:2198`）用 `app.get_webview_window("main").is_some()` 判断窗口是否还在（第 2204 行）。

但在 2026-11-11 的一次实测中：窗口已被销毁、进程存活 14 秒、**错误弹框始终没出现**。
弹框不依赖文件写入，它没出现就说明看门狗走了 `continue` 分支；
也就是说 `get_webview_window("main")` 在 HWND 已被销毁后**仍返回 `Some`**。

**这意味着看门狗目前形同虚设**——它本该兜底的那个故障，它兜不住。

**要先做的验证（不要跳过）**

1. 写一个最小复现：让 WebView2 初始化必然失败（最简单的办法是临时把 `webview2_data_dir()` 指向一个**不可写**路径，或在建窗后立刻 `DestroyWindow`）。
2. 在该场景下打印 `get_webview_window("main").is_some()`、`window.is_visible()`、`window.hwnd()` 的返回值。
3. 确认哪个信号能可靠区分「窗口健在」与「HWND 已销毁」。

**修复方向**

把判据从「管理器里有没有这条记录」改成「窗口是否真的可见／句柄是否有效」，例如：

```rust
let alive = app
    .get_webview_window("main")
    .map(|w| w.is_visible().unwrap_or(false))
    .unwrap_or(false);
```

同时注意**别引入误报**：用户如果在 10 秒内主动关窗，不能弹「启动失败」。
建议先记录「是否曾经观察到窗口可见」，只有「从未可见过」才判定为启动失败；
并把「判定逻辑」抽成纯函数，方便单测（现在的逻辑埋在闭包里，无法测）。

**验收**

- 构造 WebView2 失败场景 → 必须弹框 + 写日志 + 退出码 1，且不留僵尸进程。
- 正常启动 → 10 秒后看门狗安静退出，日志出现「主窗口稳定存在」。
- 启动后 2 秒内关闭窗口 → **不弹任何错误框**，正常退出。
- 新增单测覆盖判定函数。

**实测结论（2026-11-22，一次性 debug 探针，结论落地后探针已删除）**

在「窗口刚被 DestroyWindow、Tauri 还没来得及处理销毁事件」的同一帧采样：

| 现场 | get_webview_window | IsWindow | IsWindowVisible | hwnd() | is_visible() |
|---|---|---|---|---|---|
| 窗口健在可见 | Some | true | true | Ok(句柄) | Ok(true) |
| 窗口被销毁 | **Some（陈旧）** | false | false | Ok(旧句柄) | Ok(false) |
| 窗口在但不可见 | Some | true | false | Ok(句柄) | Ok(false) |

三条结论：

1. `get_webview_window("main").is_some()` **完全无法区分这三种情况**，它只回答「窗口表里有没有记录」，
   这就是漏报的直接原因；复现时旧判据在「窗口不可见」状态下照样打印「主窗口稳定存在，退出监控」。
2. `hwnd()` 单独用不可靠（返回的是陈旧句柄），必须对它调 `IsWindow`。
3. `is_visible()` / `hwnd()` 都是 `window_getter!`，即「发消息给事件循环 + `rx.recv()` 无超时等回包」；
   窗口从 tao 窗口表里消失后那条消息**永远没人应答**，在后台线程调用会永久阻塞——
   所以看门狗**不能用**这两个 API 轮询，只能用缓存的 HWND + Win32 直查。

**落地实现**

- `hwnd_is_alive()` / `hwnd_is_visible()`：Win32 `IsWindow` + `GetWindowThreadProcessId`（防 HWND 复用）与
  `IsWindowVisible`，不发消息、不阻塞。
- `WatchdogObservation` + `judge_watchdog()`：判定逻辑抽成**纯函数**，6 个单测覆盖
  （健康 / 句柄死亡 / 从未可见 / 用户关窗 / 取不到句柄的退化模式 / 「仅凭窗口表不得判健康」的回归）。
- 防误报靠 `seen_visible`：只要曾经看到过窗口可见，之后消失一律判「用户关窗」，不弹框。

**验证结果**

| 场景 | 手段 | 结果 |
|---|---|---|
| 从未可见 | `SW_HIDE` 在首个采样前隐藏窗口（期限缩短到 2s） | 弹框「落雪音乐下载器 启动失败」+ 日志 `[致命] 看门狗：主窗口从未可见` + **退出码 1** + 进程退出 |
| 正常启动 | 正式包连跑 14s | 日志「看门狗：主窗口稳定存在（已观察 10014ms，可见），退出监控」，无弹框 |
| 2 秒内关窗 | WM_CLOSE 关掉主窗口 | 进程正常退出 exitCode=0，**无任何错误框**、无 `[致命]` 日志 |
| 单元测试 | `cargo test` | 54 项全通过（新增 6 项） |

---

### P0-2　没有单实例保护

**现状**

`Cargo.toml` 只有 `tauri-plugin-opener` / `tauri-plugin-dialog`（lib.rs:2149-2150），
没有 `tauri-plugin-single-instance`。

**风险**

双击两次会起两个进程，共享同一个引擎目录与 WebView2 profile：

- 第二个实例可能因 profile 被占用而失败（本项目已确认会撞 `ERROR_BUSY 0x800700AA`）；
- 更糟的是两个实例**同时下载同一批歌、互相覆盖输出文件**；
- 数据目录回退逻辑会让第二个实例悄悄换一个 profile 继续跑，问题被掩盖。

**做法**

接入 [`tauri-plugin-single-instance`](https://tauri.app/plugin/single-instance/)：
第二次启动时把已有窗口 `set_focus()` / 取消最小化，然后立即退出，不要再建窗口。

**验收**

连续双击两次 → 只有一个主窗口、只有一个 `lx-tauri.exe` 进程；
第二次启动在 1 秒内退出，且不产生新的 WebView2 profile 目录。

**落地实现**

- `Cargo.toml`：`tauri-plugin-single-instance = "=2.4.5"`（**精确锁定**，原因见下）。
- `build_app()` 里**第一个**注册该插件；回调里 `unminimize() + show() + set_focus()`。
- 插件在自身 setup 阶段抢命名互斥体，抢不到就转发命令行给已有实例并 `process::exit(0)`——
  这个时机早于本应用的 setup，因此第二个进程**不会建窗、不会碰 WebView2 profile**。

**为什么锁 2.4.5 而不是最新版**

`tauri-plugin-single-instance` 2.5.x 要求 `tauri = ^2.12`，会连带把
`wry 0.55 → 0.57`、`tao 0.35 → 0.37`、`webview2-com 0.38 → 0.39` 一起升上去
（实测 Cargo.lock 变动 487 增 / 247 删）。GUI 核心栈整体跃迁对一次单实例加固来说风险过大，
而 2.4.5 只要求 `^2.10`，与当前 tauri 2.11.5 完全兼容，lock 仅新增 17 行。
**升级 tauri 到 2.12 应作为独立改动单独验证，不要和本项混在一起。**

**验证结果（正式包）**

| 指标 | 结果 |
|---|---|
| 第二次启动退出耗时 | 79 ms（要求 < 1000 ms） |
| 退出码 | 0 |
| `lx-tauri.exe` 进程数 | 1 |
| `webview2*` profile 目录数 | 1 → 1（没有新建） |
| 首个实例日志 | `单实例：检测到重复启动，已聚焦现有窗口` |

---

### P0-3　引擎解压失败会静默降级到系统 Python

**现状**

`resolve_engine()`（`lib.rs:347`）：

```rust
if let Ok(exe) = ensure_engine() { return (exe.to_string_lossy().to_string(), Vec::new()); }
("python".to_string(), vec![default_script()])   // ← 失败静默降级
```

`ensure_engine()` 的错误被 `if let Ok` 吞掉，**完全不留痕**。

**后果**

用户机器上没装 Python 时，最终报的是「启动子进程失败: 系统找不到指定的文件」之类，
而真正原因（engine.zip 损坏 / 磁盘满 / 目录无权限）永远不会出现。这类工单无法远程诊断。

**做法**

1. `resolve_engine()` 改为返回 `Result`，或在失败分支把 `ensure_engine()` 的错误写进 `startup_log`。
2. 若最终必须降级到系统 Python，在降级前记一条 `[警告] 内置引擎不可用: <原因>`。
3. 前端在引擎不可用时给出可读提示（而不是让用户对着空列表猜）。

**验收**

故意破坏 `%LOCALAPPDATA%\落雪下载\engine\`（例如把 `lx_music_downloader.exe` 改名）→
`startup.log` 必须出现具体错误原因；界面给出可读提示。

**落地实现**

- `resolve_engine()` 改为 `match ensure_engine()`，失败分支调 `note_engine_fallback(&err)`：
  用 `OnceLock<String>` 保证**只在首次失败时写日志**（`resolve_engine` 被每条命令调用，
  每次写会把 startup.log 刷爆），日志内容为 `[警告] 内置引擎不可用: <原因>` + 引擎目录 + 日志路径。
- 新增 `engine_status` 命令（async + `spawn_blocking`，首次运行要解压 43MB）返回
  `{ ok, reason, engineDir, logPath }`；前端挂载时查一次。
- 前端：不可用时在顶部显示红色横幅（原因 + 日志路径 + 处置建议），并往日志区补一条 error。

**验证结果（正式包）**

构造方式：把引擎目录整体删除，并在同级的 `engine.staging` 位置放一个普通文件
（`create_dir_all` 必然失败，等价于磁盘异常/残留文件把解压堵死）。

- `startup.log` 出现：
  `[警告] 内置引擎不可用: 创建引擎目录失败: 当文件已存在时，无法创建该文件。 (os error 183)`
  `[警告] 已降级到系统 Python（仅开发环境可用）。引擎目录: …\engine，启动日志: …\startup.log`
- 界面顶部出现红色横幅「内置下载引擎不可用，已退回系统 Python」，含原因、日志路径与处置建议，
  另有「知道了」可关闭（截图见 `.probe/engine-banner2.png`）。
- 移除破坏物后重启：引擎自动重新解压，横幅不再出现。

**注意**：验收文档里「把 `lx_music_downloader.exe` 改名」这种破坏方式其实**验不出问题**——
`ensure_engine()` 发现 `.engine_hash` 与 EXE 都不匹配时会自动重新解压，属于自愈路径。
要复现「解压失败」，得让解压本身失败（占位文件、只读目录、磁盘满）。

---

## 三、P1：用户可感知（建议 1.2.0）

| 项 | 现状 | 建议 |
|---|---|---|
| 首次启动无进度 | 解压 43MB → 103MB，界面像卡住 | 首帧显示「正在释放引擎…」，或做 splash |
| ~~孤儿引擎副本~~ ✅ | 已实现：启动后后台清理非当前 `data_root()` 下的 `engine/` | 见下方「P1-孤儿引擎副本（已完成）」 |
| 诊断难收集 | 用户要自己找 `startup.log` | 「关于」页加「复制诊断信息」按钮（版本 + WebView2 版本 + 日志尾部） |
| `--min-quality` 默认 320k | 默认不允许降到 128k | 按目标用户决定；「能听就行」场景改 128k 更实用（**需产品决策，不要擅改**） |

### P1-孤儿引擎副本清理（✅ 2026-11-22 完成）

数据目录是按可写性探测出来的，一旦从 AppData 回退到 EXE 同级，旧位置的引擎副本
（实测 111MB）就再没人删了。现在启动后在后台线程扫描全部候选数据根，
清掉**非当前** root 下带 `.engine_hash` 标记的 `engine/`：

- 只认 `.engine_hash` 标记 → 用户目录里碰巧叫 `engine` 的文件夹不会被误删；
- 只删 `engine/` 子目录 → 同级的数据库 / 日志 / 音源不动；
- 删不掉（文件被占用 / 权限不足）只记 `[警告]`，留给下次启动，绝不影响启动流程；
- 单实例保护（P0-2）保证同一时刻只有一个实例，不会删掉别的实例正在用的引擎。

实现：`orphan_engine_dirs()` / `cleanup_orphan_engines_for()` / `spawn_orphan_engine_cleanup()`，
5 项单测（含真实删除路径、标记校验、去重、当前 root 不删、释放字节统计）。

**验证说明**：删除逻辑由单测真实覆盖（同一进程创建并删除目录树）；
DSH 沙箱里 App 进程是受限令牌，连自己在 TEMP 下新建的目录都删不掉（`os error 5`），
所以运行期正向删除无法在沙箱内复现——这不是代码问题（沙箱限制见第五节）。

---

## 四、P2 / P3

**P2 工程化**

- 发布流程目前是手工 REST 调用。建议写 `scripts/release.ps1`：
  改版本号 → 构建 → 算 SHA-256 → 建 Release → 上传资产。注意 `gh` 没装，用 REST API。
- 资产命名不统一：v1.0.0 是 `lx-music-downloader-1.0.0-windows-x64.exe`，之后是纯版本号。
  自动更新靠 `assets.find(a => /\.exe$/i.test(a.name))` 挑第一个（`lx_url_server.js:2025`），
  **能工作但很脆**——多传一个 exe 就挑错。固定命名 + 收紧正则。
- 看门狗逻辑无测试覆盖，这正是它失效没被发现的原因（见 P0-1）。

**P3 功能（作者自有待办，见 HANDOVER）**

- 搜索源稳定性：酷我 `search.kuwo.cn/r.s` 是无鉴权老接口，随时可能下线。
- 歌单页日志串页：各 Tab 共用同一个 `logs` 数组。
- 代码签名：Certum 开源证书要求 GitHub 1k star（现 11），短期不现实；
  维持「README 说明 + SHA-256」即可。**不要为 SmartScreen 买 EV，微软已取消 EV 绕过机制。**

---

## 五、不要做的事（已排除的假设，别浪费时间）

- **不要**怀疑「缺少 WebView2 运行时」：`GetAvailableCoreWebView2BrowserVersionString` 返回正常，
  运行时版本探测在 `startup.log` 里能看到。
- **不要**用 HTTPS 代理探测去猜端口：Clash 是 **7896**（`127.0.0.1:7896`，已验证）。
- **不要**用 `git credential fill`：助手路径已损坏，会卡住。
- **不要**用 `cargo build --release` 的产物做发布验证：会得到 dev 页面。
- **不要**在 DSH 沙箱里断言 WebView2 坏掉：沙箱下**任何** 新建 WebView2 控制器都会失败
  （`0x800700AA`），包括别的正常应用，那是沙箱限制不是真实故障。
- **不要**以为引擎测试有历史欠账：用 Python 3.13 跑是全绿的（184/184）。

---

## 六、交付前检查表

```powershell
# 1. 引擎测试（必须用 3.13，带 mutagen）
cd lx-downloader
& "C:\Users\God Know\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe" -m unittest discover -s tests
# 期望：Ran 184 tests ... OK

# 2. 前端类型 + Rust 测试
cd ..\lx-tauri && npx tsc --noEmit
cd src-tauri && cargo test      # 期望 48+ 项全通过
cargo clippy --all-targets -- -D warnings

# 3. 正式包（不是 cargo build！）
cd ..\lx-tauri && npx tauri build --no-bundle

# 4. 启动验证：窗口 visible=True、WebView2 子进程 >0、startup.log 有「主窗口创建成功」
Get-Content "$env:LOCALAPPDATA\落雪下载\startup.log" -Tail 10

# 5. 发布（记得先设代理）
$env:HTTPS_PROXY = "http://127.0.0.1:7896"
```

发布后必须**从下载地址再拉一次**，比对 SHA-256 与本地一致，才算完成。
