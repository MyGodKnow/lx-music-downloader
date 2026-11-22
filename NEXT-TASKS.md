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

## 二、P0：三件事，按顺序做

> 原则：**先证明问题存在，再改代码**。上一轮的教训是「加了防护但没验证防护本身是否有效」。

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

---

## 三、P1：用户可感知（建议 1.2.0）

| 项 | 现状 | 建议 |
|---|---|---|
| 首次启动无进度 | 解压 43MB → 103MB，界面像卡住 | 首帧显示「正在释放引擎…」，或做 splash |
| 孤儿引擎副本 | 数据目录回退后旧位置的 ~100MB 不会被清理 | 启动时清理非当前 `data_root()` 下的 `engine/` |
| 诊断难收集 | 用户要自己找 `startup.log` | 「关于」页加「复制诊断信息」按钮（版本 + WebView2 版本 + 日志尾部） |
| `--min-quality` 默认 320k | 默认不允许降到 128k | 按目标用户决定；「能听就行」场景改 128k 更实用 |

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
