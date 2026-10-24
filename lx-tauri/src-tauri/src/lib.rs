use serde::Serialize;
use std::io::{BufReader, Read};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Emitter, Manager, State};

/// 全局状态：当前正在运行的子进程（下载或转换），每次只允许一个。
///
/// child 用 `Arc<Mutex<..>>` 而不是直接 `Mutex`：优雅停止时后台线程需要在
/// 20s 窗口内持有它等待退出，而窗口关闭时 `Drop` 仍必须能把它取出来清理。
/// 两者共享同一个 Arc，谁先取到谁负责回收，不会出现「进程退出后遗留引擎」。
#[derive(Default)]
struct TaskState {
    child: Arc<Mutex<Option<Child>>>,
    /// 当前任务的“优雅停止”信号文件路径；停止时先写该文件让引擎自行收尾。
    stop_file: Mutex<Option<std::path::PathBuf>>,
    /// 引擎是否处于「正在启动」阶段（解压引擎包 / spawn 子进程尚未完成）。
    /// 该窗口内 `stop_task` 需要返回 "starting"，否则前端会误以为任务已结束。
    starting: Arc<std::sync::atomic::AtomicBool>,
}

/// App 退出（窗口关闭）时也清理仍在运行的子进程树，避免遗留 python/node。
///
/// 注意 child 是 `Arc<Mutex<..>>`：优雅停止的后台线程可能正持有同一个 Arc
/// 在等引擎收尾。这里用 `try_lock` 避免与它僵持——拿不到锁说明后台线程
/// 正在处理，它会在自己的超时后强杀，不会漏清理。
impl Drop for TaskState {
    fn drop(&mut self) {
        let taken = match self.child.try_lock() {
            Ok(mut g) => g.take(),
            Err(std::sync::TryLockError::Poisoned(p)) => p.into_inner().take(),
            Err(std::sync::TryLockError::WouldBlock) => None,
        };
        if let Some(mut child) = taken {
            kill_process_tree(&mut child);
        }
    }
}

#[derive(Serialize, Clone)]
struct TaskEvent {
    kind: String, // "info" | "error" | "done"
    data: String,
}

/// Python 下载器脚本路径（自动探测，兼容 dev / 打包 / 不同运行目录）。
fn default_script() -> String {
    const NAME: &str = "lx_music_downloader.py";
    let mut candidates: Vec<String> = Vec::new();
    // 从当前目录与 EXE 所在目录逐级向上，在每级找 <级>/lx-downloader/lx_music_downloader.py
    let mut roots: Vec<std::path::PathBuf> = Vec::new();
    if let Ok(cwd) = std::env::current_dir() {
        roots.push(cwd);
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            roots.push(dir.to_path_buf());
        }
    }
    for root in roots {
        let mut cur = Some(root.as_path());
        while let Some(dir) = cur {
            candidates.push(dir.join("lx-downloader").join(NAME).to_string_lossy().to_string());
            candidates.push(dir.join(NAME).to_string_lossy().to_string());
            cur = dir.parent();
        }
    }

    for c in candidates {
        if std::path::Path::new(&c).exists() {
            return c;
        }
    }
    NAME.to_string()
}

// ============ 内置下载引擎（单文件 EXE 自包含） ============
// 引擎 = PyInstaller 冻结的 lx_music_downloader（内含 Python 运行时、Node、
// 音源脚本、ffmpeg），整体打成 zip 在编译期嵌进本 EXE；首次运行释放到
// %LOCALAPPDATA%\落雪下载\engine\，之后直接执行，无需安装 Python/Node。
const ENGINE_ZIP: &[u8] = include_bytes!("../engine/engine.zip");

/// 引擎释放目录（稳定位置，只在版本变化时重新释放，避免每次启动解压）。
fn engine_home() -> Option<std::path::PathBuf> {
    std::env::var_os("LOCALAPPDATA").map(std::path::PathBuf::from).map(|base| {
        base.join("落雪下载").join("engine")
    })
}

/// 内嵌引擎包的版本标识：用内容哈希而非 zip 长度。
/// 长度只在“碰巧变了”时才变，内容微调（例如只改了 lx_url_server.js）可能长度不变，
/// 会导致用户一直跑旧引擎；哈希则任何字节变化都会触发重新释放。
fn engine_version() -> String {
    let mut hasher = <sha2::Sha256 as sha2::Digest>::new();
    sha2::Digest::update(&mut hasher, ENGINE_ZIP);
    format!("{:x}", sha2::Digest::finalize(hasher))
}

/// 确保内置引擎已释放到本地；版本变化（内容哈希不同）时重新释放。
/// 升级采用「解压到临时目录 → 目录交换」的方式：正在运行的旧引擎文件被占用时
/// 也能完成升级，不会因为删不掉旧目录而卡在旧版本。
fn ensure_engine() -> Result<std::path::PathBuf, String> {
    let dir = engine_home().ok_or_else(|| "无法定位 LOCALAPPDATA".to_string())?;
    let exe = dir.join("lx_music_downloader.exe");
    let marker = dir.join(".engine_hash");
    let ver = engine_version();
    let fresh = exe.is_file()
        && std::fs::read_to_string(&marker)
            .map(|v| v.trim() == ver)
            .unwrap_or(false);
    if fresh {
        return Ok(exe);
    }

    // 解压到同级临时目录，成功后再替换正式目录
    let staging = dir.with_extension("staging");
    let backup = dir.with_extension("old");
    let _ = std::fs::remove_dir_all(&staging);
    std::fs::create_dir_all(&staging).map_err(|e| format!("创建引擎目录失败: {e}"))?;
    let cursor = std::io::Cursor::new(ENGINE_ZIP);
    let mut archive = zip::ZipArchive::new(cursor).map_err(|e| format!("内置引擎包损坏: {e}"))?;
    archive
        .extract(&staging)
        .map_err(|e| format!("释放内置引擎失败: {e}"))?;
    std::fs::write(staging.join(".engine_hash"), &ver)
        .map_err(|e| format!("写入引擎版本标记失败: {e}"))?;

    // 目录交换：旧目录先挪开，再把新目录改名到位；最后尽力清理旧目录。
    let _ = std::fs::remove_dir_all(&backup);
    if dir.exists() {
        std::fs::rename(&dir, &backup).map_err(|e| format!("替换引擎目录失败: {e}"))?;
    }
    std::fs::rename(&staging, &dir).map_err(|e| {
        // 换名失败时尽量把旧目录挪回来，保证下次仍可启动
        let _ = std::fs::rename(&backup, &dir);
        format!("启用新引擎失败: {e}")
    })?;
    // 旧引擎可能仍被正在运行的进程占用，删不掉也无妨（下次启动会再试）
    let _ = std::fs::remove_dir_all(&backup);

    Ok(dir.join("lx_music_downloader.exe"))
}

/// 解析下载引擎命令，返回 (程序, 前置参数)：
///   1. 用户显式指定了 python 路径 → 开发模式：python + 脚本
///   2. 内置引擎可用（默认） → 冻结 EXE，无前置参数
///   3. 兜底 → 系统 python + 脚本（仅开发环境能用）
fn resolve_engine(python: Option<&String>) -> (String, Vec<String>) {
    if let Some(p) = python {
        let t = p.trim();
        if !t.is_empty() {
            return (t.to_string(), vec![default_script()]);
        }
    }
    if let Ok(exe) = ensure_engine() {
        return (exe.to_string_lossy().to_string(), Vec::new());
    }
    ("python".to_string(), vec![default_script()])
}

/// 结束子进程并连带清理其子孙进程（Python 会派生 node）。
///
/// 三层保险，任何一层都不允许无限阻塞：
///   1. `taskkill /T /F` 放到独立线程执行，主流程最多等 2s —— 安全软件拦截
///      taskkill 时它是同步 `output()` 会永久挂起，绝不能让它拖住调用方
///      （调用方可能是主线程或窗口关闭路径）
///   2. `child.kill()` 直接 TerminateProcess，不依赖外部命令
///   3. 回收最多等 3s，超时放弃（进程已被杀，句柄随 Child drop 释放）
fn kill_process_tree(child: &mut Child) {
    let pid = child.id();

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        // 独立线程跑 taskkill：即使被拦截挂起，也不会阻塞本函数
        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let _ = Command::new("taskkill")
                .args(["/PID", &pid.to_string(), "/T", "/F"])
                .creation_flags(CREATE_NO_WINDOW)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .output();
            let _ = tx.send(());
        });
        // 最多等 2 秒；等不到就继续走 child.kill()
        let _ = rx.recv_timeout(std::time::Duration::from_secs(2));
    }
    #[cfg(not(windows))]
    {
        let _ = pid;
    }

    let _ = child.kill();

    // 最多等 3 秒回收，超时则放弃（进程已被 kill，句柄随 Child drop 释放）
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(3);
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) => {}
            Err(_) => break,
        }
        if std::time::Instant::now() >= deadline {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
}

/// 校验 python 路径是否为一个可信的解释器可执行文件。
/// 空串视为默认（不校验）；非解释器名或路径不存在则拒绝，避免任意命令执行。
fn validate_python(python: Option<&String>) -> Result<(), String> {
    let Some(p) = python else { return Ok(()) };
    let t = p.trim();
    if t.is_empty() {
        return Ok(());
    }
    let path = std::path::Path::new(t);
    if !path.is_file() {
        return Err(format!("python 路径不存在: {t}"));
    }
    let base = path
        .file_name()
        .map(|n| n.to_string_lossy().to_lowercase())
        .unwrap_or_default();
    let ok_names = ["python.exe", "pythonw.exe", "py.exe", "python", "pythonw", "py"];
    if !ok_names.contains(&base.as_str()) {
        return Err("python 路径必须是 python/pythonw/py 可执行文件".to_string());
    }
    Ok(())
}

/// 统一拒绝以 `-` 开头的命令行参数值，防止被 argparse 当作选项吞掉。
fn reject_flag_prefix(label: &str, value: &str) -> Result<(), String> {
    if value.trim_start().starts_with('-') {
        return Err(format!("{label} 不能以 - 开头"));
    }
    Ok(())
}

/// 音质档位白名单（与引擎 QUALITY_OPTIONS 的键一致）。
///
/// 这是「值域校验」而非「前缀校验」：`quality` 是直接拼进 argv 的值，
/// 若只挡 `-` 开头，像 `flac --dir C:\Windows\System32` 这种仍会被 argparse
/// 解析成第二个 `--dir` 并覆盖下载落点。白名单能彻底杜绝这类注入。
const QUALITY_WHITELIST: [&str; 5] = ["128k", "192k", "320k", "flac", "flac24bit"];

/// 校验音质参数；空值表示「用引擎默认值」，也允许。
fn validate_quality(quality: &str) -> Result<(), String> {
    let q = quality.trim();
    if q.is_empty() || QUALITY_WHITELIST.contains(&q) {
        return Ok(());
    }
    Err(format!("不支持的音质: {quality}"))
}

/// 统一的分行日志读取线程：按行缓冲并解码，避免多字节字符被块边界截断。
/// emit_done=true 时在流结束后推送一次 "done" 事件。
fn spawn_log_reader<R: Read + Send + 'static>(
    app: AppHandle,
    event_name: String,
    stream: R,
    emit_done: bool,
) {
    std::thread::spawn(move || {
        let mut buf = [0u8; 8192];
        let mut reader = BufReader::new(stream);
        let mut leftover: Vec<u8> = Vec::new();
        loop {
            let n = match reader.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => n,
                Err(_) => break,
            };
            leftover.extend_from_slice(&buf[..n]);
            while let Some(pos) = leftover.iter().position(|&b| b == b'\n') {
                let line = leftover.drain(..=pos).collect::<Vec<_>>();
                emit_line(&app, &event_name, &line);
            }
        }
        if !leftover.is_empty() {
            emit_line(&app, &event_name, &leftover);
        }
        if emit_done {
            let _ = app.emit(event_name.as_str(), TaskEvent {
                kind: "done".to_string(),
                data: "任务已结束".to_string(),
            });
        }
    });
}

/// 按行首结构化标记归类日志，避免子串误判。
fn classify_log(decoded: &str) -> &'static str {
    let t = decoded.trim_start();
    const ERR_TAGS: [&str; 4] = ["[致命错误]", "[错误]", "[失败]", "[停止]"];
    const OK_TAGS: [&str; 2] = ["[成功]", "[转换完成]"];
    if ERR_TAGS.iter().any(|k| t.starts_with(k)) {
        "error"
    } else if OK_TAGS.iter().any(|k| t.starts_with(k)) {
        "ok"
    } else {
        "info"
    }
}

/// 带超时运行命令并收集输出。超时返回 None，并连带清理子进程树。
/// 用于 list_playlists 等一次性命令，避免阻塞 UI 无限等待。
fn run_command_output(
    command: &mut Command,
    timeout_secs: u64,
) -> Result<Option<std::process::Output>, String> {
    // 本函数依赖 child.stdout/stderr 读取输出，必须管道化（否则 None → panic）；
    // stdin 置空避免子进程等待交互，同时隐藏控制台窗口。
    command
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .stdin(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    let mut child = command
        .spawn()
        .map_err(|e| format!("启动子进程失败: {e}"))?;
    let stdout = child.stdout.take().expect("no stdout");
    let stderr = child.stderr.take().expect("no stderr");
    let out_buf: std::sync::Arc<std::sync::Mutex<Vec<u8>>> = Default::default();
    let err_buf: std::sync::Arc<std::sync::Mutex<Vec<u8>>> = Default::default();
    let drain = |stream: Box<dyn Read + Send>, buf: std::sync::Arc<std::sync::Mutex<Vec<u8>>>| {
        std::thread::spawn(move || {
            let mut r = BufReader::new(stream);
            let mut chunk = [0u8; 8192];
            loop {
                match r.read(&mut chunk) {
                    Ok(0) => break,
                    Ok(n) => {
                        if let Ok(mut g) = buf.lock() {
                            g.extend_from_slice(&chunk[..n]);
                        }
                    }
                    Err(_) => break,
                }
            }
        });
    };
    drain(Box::new(stdout), out_buf.clone());
    drain(Box::new(stderr), err_buf.clone());

    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(timeout_secs);
    let status = loop {
        if let Some(st) = child
            .try_wait()
            .map_err(|e| format!("等待子进程失败: {e}"))?
        {
            break st;
        }
        if std::time::Instant::now() >= deadline {
            kill_process_tree(&mut child);
            return Ok(None);
        }
        std::thread::sleep(std::time::Duration::from_millis(100));
    };
    Ok(Some(std::process::Output {
        status,
        stdout: out_buf.lock().map(|g| g.to_vec()).unwrap_or_default(),
        stderr: err_buf.lock().map(|g| g.to_vec()).unwrap_or_default(),
    }))
}

/// 优雅停止信号文件：GUI 点「停止」时由本进程写入，引擎侧轮询到后自行收尾
/// （停止派发新任务 → 在途任务落盘 → 写 failed.txt → 正常退出）。
/// 只有超过等待上限仍未退出时才强制杀进程树。
fn stop_signal_path() -> std::path::PathBuf {
    std::env::temp_dir().join("lx-downloader-stop.flag")
}

/// 等待引擎优雅退出的上限（秒）。超过则强制结束整棵进程树。
const GRACEFUL_STOP_WAIT_SECS: u64 = 20;

/// 组装命令行，启动子进程并起读取线程推日志。
/// engine = (程序, 前置参数)：内置引擎直接执行 EXE；开发模式为 python + 脚本。
fn spawn_task(
    app: AppHandle,
    state: &TaskState,
    event_name: &'static str,
    args: Vec<String>,
    engine: (String, Vec<String>),
) -> Result<String, String> {
    // 先取走上一次任务的 child，**在锁外**再杀。
    // 绝不能持锁调用 kill_process_tree：它内部有 taskkill 与进程回收，
    // 持锁期间会堵死 stop_task / 窗口退出等所有路径。
    let old_child = state.child.lock().ok().and_then(|mut g| g.take());
    if let Some(mut old) = old_child {
        kill_process_tree(&mut old);
    }

    // 清理上一次运行残留的停止信号，避免新任务一启动就被当成“停止”。
    let stop_path = stop_signal_path();
    let _ = std::fs::remove_file(&stop_path);

    // 标记「正在启动」：此时还没有 child，stop_task 需要据此返回 "starting"
    // 而不是 "idle"（否则前端会误判任务已结束）。
    state
        .starting
        .store(true, std::sync::atomic::Ordering::SeqCst);

    let mut cmd = Command::new(&engine.0);
    cmd.args(&engine.1)
        .args(&args)
        .arg("--stop-file")
        .arg(&stop_path);
    cmd.stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .stdin(Stdio::null());
    // 后台静默运行：不弹出命令提示符窗口
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }

    let spawned = cmd.spawn();
    // 无论成败都清除启动标记，避免后续 stop 一直显示 "starting"
    state
        .starting
        .store(false, std::sync::atomic::Ordering::SeqCst);
    let mut child = spawned.map_err(|e| format!("启动子进程失败: {e}"))?;
    let stdout = child.stdout.take().expect("no stdout");
    let stderr = child.stderr.take().expect("no stderr");
    if let Ok(mut guard) = state.child.lock() {
        *guard = Some(child);
    }
    if let Ok(mut guard) = state.stop_file.lock() {
        *guard = Some(stop_path);
    }

    // stdout（任务日志，流结束推 done）+ stderr（诊断）统一走分行读取线程
    spawn_log_reader(app.clone(), event_name.to_string(), stdout, true);
    spawn_log_reader(app, event_name.to_string(), stderr, false);
    Ok("started".into())
}

fn emit_line(app: &AppHandle, event_name: &str, bytes: &[u8]) {
    let line = trim_end(bytes);
    if line.is_empty() {
        return;
    }
    let decoded = decode_line(&line);
    if decoded.trim().is_empty() {
        return;
    }
    // 结构化进度事件：单独发射，不进入日志区
    if let Some(rest) = decoded.strip_prefix("##PROGRESS## ") {
        let _ = app.emit("progress", rest.trim().to_string());
        return;
    }
    let _ = app.emit(event_name, TaskEvent {
        kind: classify_log(&decoded).to_string(),
        data: decoded,
    });
}

fn trim_end(bytes: &[u8]) -> Vec<u8> {
    let mut v = bytes.to_vec();
    while v.last().map(|&b| b == b'\n' || b == b'\r').unwrap_or(false) {
        v.pop();
    }
    v
}

fn decode_line(bytes: &[u8]) -> String {
    if let Ok(s) = std::str::from_utf8(bytes) {
        return s.to_string();
    }
    let (enc, _, _) = encoding_rs::GBK.decode(bytes);
    enc.into_owned()
}

#[derive(Serialize, Clone)]
struct Playlist {
    id: String,
    name: String,
    cnt: usize,
}

/// 探测数据库 / 导入歌单并返回可用歌单列表。
///
/// 该命令最多阻塞 60s（等引擎子进程）。Tauri 2 的同步命令占用主线程会冻结界面，
/// 因此改为 async + spawn_blocking，把阻塞工作挪到线程池。
#[tauri::command]
async fn list_playlists(db: Option<String>, python: Option<String>) -> Result<Vec<Playlist>, String> {
    tauri::async_runtime::spawn_blocking(move || list_playlists_blocking(db, python))
        .await
        .map_err(|e| format!("任务执行失败: {e}"))?
}

fn list_playlists_blocking(
    db: Option<String>,
    python: Option<String>,
) -> Result<Vec<Playlist>, String> {
    validate_python(python.as_ref())?;
    if let Some(d) = &db {
        let t = d.trim();
        if !t.is_empty() {
            reject_flag_prefix("数据库路径", t)?;
        }
    }
    let mut args: Vec<String> = vec![];
    if let Some(d) = db {
        if !d.trim().is_empty() {
            args.push("--db".into());
            args.push(d.trim().into());
        }
    }
    args.push("--list".into());
    let (program, prefix) = resolve_engine(python.as_ref());
    let mut cmd = Command::new(&program);
    cmd.args(&prefix).args(&args);
    let out = match run_command_output(&mut cmd, 60) {
        Ok(Some(o)) => o,
        Ok(None) => {
            return Err("列出歌单超时（60s），请检查 python 与落雪数据库后重试".to_string());
        }
        Err(e) => return Err(e),
    };
    let text = decode_line(&out.stdout);
    let stderr_text = decode_line(&out.stderr);
    if !out.status.success() {
        let msg = if stderr_text.contains("未找到") || text.contains("未找到") {
            "未找到落雪音乐数据库，请先启动落雪音乐桌面版，或在页面导入 .lxmc 歌单文件。".to_string()
        } else {
            format!("{}{}", text, stderr_text)
        };
        return Err(msg);
    }
    parse_playlists(&text)
}

fn parse_playlists(stdout: &str) -> Result<Vec<Playlist>, String> {
    let mut playlists = Vec::new();
    let re = regex::Regex::new(r"\[\s*([^\]]+?)\s*\]\s*(.+?)\s*\((\d+)\s*首\)").unwrap();
    for cap in re.captures_iter(stdout) {
        let id = cap[1].trim().to_string();
        let name = cap[2].trim().to_string();
        let cnt: usize = cap[3].parse().unwrap_or(0);
        playlists.push(Playlist { id, name, cnt });
    }
    if playlists.is_empty() {
        return Err("未能解析到歌单。若数据库已更换，请在上传歌单处导入。".to_string());
    }
    Ok(playlists)
}

/// 开始下载歌单。
/// 参数较多是 Tauri 命令的固有形态（前端按名字逐个传），不宜合并成结构体。
///
/// 必须 async：`resolve_engine` 在首次运行时会同步解压 41MB 内嵌引擎包，
/// 而同步命令跑在主线程，会造成启动即卡死。这里用 AppHandle（'static）取状态，
/// 绕开 `State<'_, _>` 无法移入 spawn_blocking 闭包的生命周期限制。
#[allow(clippy::too_many_arguments)]
#[tauri::command]
async fn start_download(
    app: AppHandle,
    playlist: String,
    quality: String,
    dir: Option<String>,
    workers: usize,
    fallback: bool,
    min_quality: Option<String>,
    db: Option<String>,
    python: Option<String>,
) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        start_download_blocking(
            app, playlist, quality, dir, workers, fallback, min_quality, db, python,
        )
    })
    .await
    .map_err(|e| format!("任务执行失败: {e}"))?
}

#[allow(clippy::too_many_arguments)]
fn start_download_blocking(
    app: AppHandle,
    playlist: String,
    quality: String,
    dir: Option<String>,
    workers: usize,
    fallback: bool,
    min_quality: Option<String>,
    db: Option<String>,
    python: Option<String>,
) -> Result<String, String> {
    validate_python(python.as_ref())?;
    let playlist = playlist.trim();
    if playlist.is_empty() {
        return Err("歌单不能为空".to_string());
    }
    reject_flag_prefix("歌单 id", playlist)?;
    // quality 直接进 argv，必须做值域白名单（仅挡 `-` 前缀不足以防 `flac --dir X`）
    validate_quality(&quality)?;
    // 统一校验：进入 argv 的空字符串值不得以 `-` 开头
    for (label, v) in [
        ("下载目录", dir.as_deref().unwrap_or("").trim()),
        ("min_quality", min_quality.as_deref().unwrap_or("").trim()),
        ("数据库路径", db.as_deref().unwrap_or("").trim()),
    ] {
        if !v.is_empty() {
            reject_flag_prefix(label, v)?;
        }
        if label == "min_quality" {
            validate_quality(v)?;
        }
    }
    let mut args = vec!["--playlist".into(), playlist.to_string(), "--quality".into(), quality];
    if let Some(d) = dir {
        if !d.trim().is_empty() {
            args.push("--dir".into());
            args.push(d.trim().into());
        }
    }
    if workers > 0 {
        args.push("--workers".into());
        args.push(workers.to_string());
    }
    if fallback {
        args.push("--fallback-quality".into());
        if let Some(mq) = min_quality {
            if !mq.trim().is_empty() {
                args.push("--min-quality".into());
                args.push(mq.trim().into());
            }
        }
    }
    if let Some(d) = db {
        if !d.trim().is_empty() {
            args.push("--db".into());
            args.push(d.trim().into());
        }
    }
    let engine = resolve_engine(python.as_ref());
    let state = app.state::<TaskState>();
    spawn_task(app.clone(), &state, "download-log", args, engine)
}

/// 停止当前任务：先写停止信号让引擎自行收尾，后台线程等待，超时才强杀进程树。
///
/// 关键点：**绝不能在这个命令里同步等待**。
/// Tauri 2 的同步命令运行在主线程，任何 sleep/轮询都会冻结整个窗口
/// （表现就是「点停止后界面卡死，再点音源页无响应」）。
/// 因此这里只做三件立刻返回的事：
///   1. 把 child 从状态里取走（之后新任务不会再被它干扰）
///   2. 写停止信号文件
///   3. 交给后台线程去等 / 超时强杀
#[tauri::command]
fn stop_task(state: State<'_, TaskState>) -> Result<String, String> {
    let stop_path = state
        .stop_file
        .lock()
        .ok()
        .and_then(|g| g.clone())
        .unwrap_or_else(stop_signal_path);

    // 引擎还在启动阶段（解压引擎包 / spawn 未完成）时没有 child，
    // 不能直接报 idle —— 否则前端会把 running 置 false 并允许再点开始，
    // 与仍在启动的那次任务撞车。
    if state
        .starting
        .load(std::sync::atomic::Ordering::SeqCst)
    {
        // 让引擎在启动后立刻看到停止信号，收尾退出
        let _ = std::fs::write(&stop_path, b"stop\n");
        return Ok("starting".to_string());
    }

    // 用 Arc 共享 child：后台线程拿走等待权，但 Drop 仍能在窗口关闭时清理。
    let child_arc = state.child.clone();
    let has_child = child_arc
        .lock()
        .map(|g| g.is_some())
        .unwrap_or(false);
    if !has_child {
        return Ok("idle".to_string());
    }

    // 1) 写停止信号，交给引擎自己收尾（落盘 + 写失败清单 + 清理 .part）
    let graceful = std::fs::write(&stop_path, b"stop\n").is_ok();

    // 2) 立即返回，等待与兜底强杀放到后台线程
    std::thread::spawn(move || {
        if graceful {
            let deadline = std::time::Instant::now()
                + std::time::Duration::from_secs(GRACEFUL_STOP_WAIT_SECS);
            loop {
                // 每轮短暂持锁只做 try_wait，避免长时间占住 Arc
                let state_now = {
                    let mut guard = match child_arc.lock() {
                        Ok(g) => g,
                        Err(p) => p.into_inner(),
                    };
                    match guard.as_mut() {
                        Some(c) => match c.try_wait() {
                            Ok(Some(_)) => {
                                // 引擎已自行退出：取走 child，Drop 不会再碰它
                                *guard = None;
                                Some(true)
                            }
                            Ok(None) => Some(false),
                            Err(_) => Some(true),
                        },
                        None => Some(true), // 已被别处取走（退出路径），结束等待
                    }
                };
                if state_now != Some(false) {
                    return;
                }
                if std::time::Instant::now() >= deadline {
                    break;
                }
                std::thread::sleep(std::time::Duration::from_millis(150));
            }
        }
        // 3) 兜底：超时仍未退出，强杀整棵进程树
        let mut guard = match child_arc.lock() {
            Ok(g) => g,
            Err(p) => p.into_inner(),
        };
        if let Some(mut child) = guard.take() {
            drop(guard); // 锁外杀，避免持锁调用 taskkill
            kill_process_tree(&mut child);
        }
    });

    Ok(if graceful { "stopping" } else { "killing" }.to_string())
}

/// 查询当前是否有任务在跑（供前端在「正在停止」时做早期校正）。
/// 纯内存读，不会阻塞。
#[tauri::command]
fn is_task_running(state: State<'_, TaskState>) -> bool {
    if state
        .starting
        .load(std::sync::atomic::Ordering::SeqCst)
    {
        return true;
    }
    let mut guard = match state.child.lock() {
        Ok(g) => g,
        Err(p) => p.into_inner(),
    };
    match guard.as_mut() {
        None => false,
        // 已退出的子进程顺手回收，并报告「没在跑」
        Some(child) => match child.try_wait() {
            Ok(Some(_)) => {
                *guard = None;
                false
            }
            Ok(None) => true,
            Err(_) => false,
        },
    }
}

// ============ 音源管理 ============
// 音源脚本由用户自行导入，统一存放于 %APPDATA%\lx-downloader-sources（与 Python 引擎
// 的 USER_SOURCES_DIR 一致）。仓库与程序更新不会覆盖该目录，用户音源长期保留。
fn user_sources_dir() -> std::path::PathBuf {
    let base = std::env::var_os("APPDATA").unwrap_or_else(|| ".".into());
    std::path::PathBuf::from(base).join("lx-downloader-sources")
}

/// 音源文件名校验：只保留 basename 并限定 .js/.json，防止目录穿越。
fn validate_source_name(raw: &str) -> Result<String, String> {
    let name = std::path::Path::new(raw)
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_default();
    if name.is_empty() || name == "." || name == ".." {
        return Err("音源文件名非法".to_string());
    }
    let lower = name.to_lowercase();
    if !lower.ends_with(".js") && !lower.ends_with(".json") {
        return Err("仅支持 .js / .json 音源文件".to_string());
    }
    Ok(name)
}

/// 返回用户音源目录（供界面展示）。
#[tauri::command]
fn get_sources_dir() -> String {
    user_sources_dir().to_string_lossy().to_string()
}

/// 音源目录里属于「程序自己的元数据文件」，不是音源，任何列表/排序/加载都要排除。
///
/// `.order.json` 存拖拽顺序，`.health.json` 存音源健康度。两者都是 `.json`，
/// 若不显式排除会被当成音源脚本：既会出现在音源列表里让用户困惑，
/// 又会被 Node 当脚本加载（内容不是合法音源，白白报错）。
fn is_meta_file(name: &str) -> bool {
    matches!(name, ".order.json" | ".health.json" | ".probe.json")
}

/// 列出用户已导入的音源文件。
#[tauri::command]
fn list_sources() -> Vec<String> {
    let dir = user_sources_dir();
    let mut out: Vec<String> = Vec::new();
    if let Ok(rd) = std::fs::read_dir(&dir) {
        for entry in rd.flatten() {
            let name = entry.file_name().to_string_lossy().to_string();
            let lower = name.to_lowercase();
            if (lower.ends_with(".js") || lower.ends_with(".json")) && !is_meta_file(&name) {
                out.push(name);
            }
        }
    }
    out.sort();
    out
}

/// 导入音源：把前端传来的音源内容写入用户音源目录。
///
/// 用 async + spawn_blocking：混淆音源可达数 MB，同步写 + 杀软扫描可能
/// 让主线程卡上几百毫秒甚至更久。
#[tauri::command]
async fn import_source(file_name: String, content: String) -> Result<String, String> {
    let name = validate_source_name(&file_name)?;
    let dir = user_sources_dir();
    let target = dir.join(&name);
    tauri::async_runtime::spawn_blocking(move || {
        std::fs::create_dir_all(&dir).map_err(|e| format!("创建音源目录失败: {e}"))?;
        std::fs::write(&target, content).map_err(|e| format!("写入音源失败: {e}"))
    })
    .await
    .map_err(|e| format!("导入任务执行失败: {e}"))??;
    Ok(name)
}

/// 删除用户已导入的音源。
#[tauri::command]
async fn remove_source(file_name: String) -> Result<(), String> {
    let name = validate_source_name(&file_name)?;
    let dest = user_sources_dir().join(&name);
    tauri::async_runtime::spawn_blocking(move || {
        if dest.exists() {
            std::fs::remove_file(&dest).map_err(|e| format!("删除音源失败: {e}"))?;
        }
        Ok::<(), String>(())
    })
    .await
    .map_err(|e| format!("删除任务执行失败: {e}"))?
}

// ============ 音源优先级（拖拽排序） ============
// 顺序存成 `%APPDATA%\lx-downloader-sources\.order.json`（字符串数组，按文件名）。
// Node 桥接在加载音源时会先读这个文件决定顺序；没有则用内置优先级。
// 这样排序结果是「用户意图」，不会被程序更新覆盖。

fn source_order_file() -> std::path::PathBuf {
    user_sources_dir().join(".order.json")
}

/// 读取用户设定的音源顺序（仅返回当前实际存在的文件，避免残留旧文件名）。
#[tauri::command]
fn get_source_order() -> Vec<String> {
    let dir = user_sources_dir();
    let existing: Vec<String> = std::fs::read_dir(&dir)
        .map(|rd| {
            rd.flatten()
                .map(|e| e.file_name().to_string_lossy().to_string())
                .filter(|n| {
                    let l = n.to_lowercase();
                    (l.ends_with(".js") || l.ends_with(".json")) && !is_meta_file(n)
                })
                .collect()
        })
        .unwrap_or_default();

    let mut ordered: Vec<String> = Vec::new();
    if let Ok(raw) = std::fs::read_to_string(source_order_file()) {
        if let Ok(list) = serde_json::from_str::<Vec<String>>(&raw) {
            for name in list {
                if existing.iter().any(|e| e == &name) && !ordered.contains(&name) {
                    ordered.push(name);
                }
            }
        }
    }
    // 未纳入顺序的新文件按名称追加，保证列表完整
    let mut rest: Vec<String> = existing
        .into_iter()
        .filter(|e| !ordered.contains(e))
        .collect();
    rest.sort();
    ordered.extend(rest);
    ordered
}

/// 保存音源顺序（GUI 拖拽后调用）。
#[tauri::command]
fn set_source_order(order: Vec<String>) -> Result<(), String> {
    let dir = user_sources_dir();
    std::fs::create_dir_all(&dir).map_err(|e| format!("创建音源目录失败: {e}"))?;
    // 只接受合法音源文件名，防止写入任意路径
    let mut clean: Vec<String> = Vec::new();
    for raw in order {
        let name = validate_source_name(&raw)?;
        if !clean.contains(&name) {
            clean.push(name);
        }
    }
    let json = serde_json::to_string_pretty(&clean)
        .map_err(|e| format!("序列化音源顺序失败: {e}"))?;
    std::fs::write(source_order_file(), json).map_err(|e| format!("保存音源顺序失败: {e}"))?;
    Ok(())
}

// ============ 从落雪音乐桌面端导入音源 ============
// 落雪桌面版把用户在 App 内添加的音源存在 %APPDATA%\lx-music-desktop\LxDatas\user_api.json，
// 结构为 {"userApis":[{"name","version","author","script":"gz_<base64(zlib)>"|"<js>"}]}。
// 这里把其中每个音源拆成独立的 .js 文件写入本程序的音源目录，
// 这样引擎就能直接加载，无需用户手工复制。

/// 落雪桌面版 user_api.json 的候选路径（官方版 / 便携版 / 中文目录名）。
fn lx_desktop_api_candidates() -> Vec<std::path::PathBuf> {
    let appdata = std::env::var_os("APPDATA").map(std::path::PathBuf::from);
    let mut out = Vec::new();
    if let Some(base) = appdata {
        out.push(base.join("lx-music-desktop").join("LxDatas").join("user_api.json"));
        out.push(base.join("lx-music-desktop").join("user_api.json"));
        out.push(base.join("洛雪音乐").join("LxDatas").join("user_api.json"));
    }
    out
}

/// 探测落雪桌面版音源文件，返回存在的第一个路径。
#[tauri::command]
fn find_lx_desktop_api() -> Option<String> {
    lx_desktop_api_candidates()
        .into_iter()
        .find(|p| p.is_file())
        .map(|p| p.to_string_lossy().to_string())
}

/// 解码落雪音源脚本：gz_ 前缀是 base64(zlib(deflate))，其余为明文 JS。
fn decode_lx_script(script: &str) -> Result<String, String> {
    if let Some(rest) = script.strip_prefix("gz_") {
        use base64::Engine;
        let raw = base64::engine::general_purpose::STANDARD
            .decode(rest.trim())
            .map_err(|e| format!("音源 base64 解码失败: {e}"))?;
        let mut out = Vec::new();
        // 限制解压后的最大字节数：user_api.json 来自用户导入的文件，
        // 恶意构造的 zlib 压缩炸弹可在几 KB 内解出 GB 级数据，直接把内存打满。
        // 正常音源（含混淆）最大也就几 MB，64MB 足够且安全。
        const MAX_SCRIPT_BYTES: u64 = 64 * 1024 * 1024;
        let mut decoder = flate2::read::ZlibDecoder::new(raw.as_slice());
        let mut limited = std::io::Read::take(&mut decoder, MAX_SCRIPT_BYTES + 1);
        std::io::Read::read_to_end(&mut limited, &mut out)
            .map_err(|e| format!("音源解压失败: {e}"))?;
        if out.len() as u64 > MAX_SCRIPT_BYTES {
            return Err("音源解压后过大（疑似压缩炸弹），已拒绝".to_string());
        }
        String::from_utf8(out).map_err(|e| format!("音源不是合法 UTF-8: {e}"))
    } else {
        Ok(script.to_string())
    }
}

/// 从落雪桌面端导入全部音源，返回导入的文件名列表。
/// 解压大音源可能耗时，改 async + spawn_blocking 避免冻结界面。
#[tauri::command]
async fn import_lx_desktop_sources(path: Option<String>) -> Result<Vec<String>, String> {
    tauri::async_runtime::spawn_blocking(move || import_lx_desktop_sources_impl(path))
        .await
        .map_err(|e| format!("任务执行失败: {e}"))?
}

fn import_lx_desktop_sources_impl(path: Option<String>) -> Result<Vec<String>, String> {
    let src = match path {
        Some(p) if !p.trim().is_empty() => std::path::PathBuf::from(p.trim()),
        _ => lx_desktop_api_candidates()
            .into_iter()
            .find(|p| p.is_file())
            .ok_or_else(|| "未找到落雪音乐桌面版的音源文件（user_api.json）。请先在落雪 App 内添加音源，或用「导入音源」手动选择文件。".to_string())?,
    };
    if !src.is_file() {
        return Err(format!("文件不存在: {}", src.display()));
    }
    let raw = std::fs::read_to_string(&src).map_err(|e| format!("读取音源文件失败: {e}"))?;
    let parsed: serde_json::Value =
        serde_json::from_str(&raw).map_err(|e| format!("音源文件不是合法 JSON: {e}"))?;
    let apis = parsed
        .get("userApis")
        .and_then(|v| v.as_array())
        .ok_or_else(|| "音源文件缺少 userApis 数组（可能不是落雪导出的 user_api.json）".to_string())?;
    if apis.is_empty() {
        return Err("落雪音源列表为空：请先在落雪音乐 App 内添加音源".to_string());
    }

    let dir = user_sources_dir();
    std::fs::create_dir_all(&dir).map_err(|e| format!("创建音源目录失败: {e}"))?;

    let mut imported = Vec::new();
    let mut errors = Vec::new();
    for (i, api) in apis.iter().enumerate() {
        let name = api.get("name").and_then(|v| v.as_str()).unwrap_or("");
        let script = api.get("script").and_then(|v| v.as_str()).unwrap_or("");
        if script.trim().is_empty() {
            errors.push(format!("第 {} 个音源没有脚本内容", i + 1));
            continue;
        }
        let js = match decode_lx_script(script) {
            Ok(s) => s,
            Err(e) => {
                errors.push(format!("{}: {e}", if name.is_empty() { format!("第{}个", i + 1) } else { name.to_string() }));
                continue;
            }
        };
        // 文件名取音源名并做安全清理；重名时加序号
        let base = if name.trim().is_empty() {
            format!("lx-source-{}", i + 1)
        } else {
            name.trim().to_string()
        };
        let safe = sanitize_file_stem(&base);
        let mut file_name = format!("{safe}.js");
        let mut n = 2;
        while dir.join(&file_name).exists() {
            file_name = format!("{safe}-{n}.js");
            n += 1;
        }
        std::fs::write(dir.join(&file_name), js)
            .map_err(|e| format!("写入音源 {file_name} 失败: {e}"))?;
        imported.push(file_name);
    }

    if imported.is_empty() {
        return Err(if errors.is_empty() {
            "没有可导入的音源".to_string()
        } else {
            format!("导入失败：{}", errors.join("；"))
        });
    }
    Ok(imported)
}

/// 把任意字符串清理成安全的文件名主体（去掉 Windows 非法字符与控制字符）。
fn sanitize_file_stem(raw: &str) -> String {
    let cleaned: String = raw
        .chars()
        .map(|c| match c {
            '\\' | '/' | ':' | '*' | '?' | '"' | '<' | '>' | '|' => '_',
            c if (c as u32) < 0x20 => '_',
            c => c,
        })
        .collect();
    let trimmed = cleaned.trim().trim_matches('.').trim();
    if trimmed.is_empty() {
        "lx-source".to_string()
    } else {
        // 文件名过长会写入失败，截断到 80 字符
        trimmed.chars().take(80).collect()
    }
}

/// 导出歌单为“歌手 - 歌名”清单（去重、按歌单原顺序）。
/// 最多阻塞 60s（等引擎），改 async + spawn_blocking 避免冻结界面。
#[tauri::command]
async fn export_songs(
    playlist: String,
    db: Option<String>,
    python: Option<String>,
) -> Result<Vec<String>, String> {
    tauri::async_runtime::spawn_blocking(move || export_songs_blocking(playlist, db, python))
        .await
        .map_err(|e| format!("任务执行失败: {e}"))?
}

fn export_songs_blocking(
    playlist: String,
    db: Option<String>,
    python: Option<String>,
) -> Result<Vec<String>, String> {
    validate_python(python.as_ref())?;
    let playlist = playlist.trim();
    if playlist.is_empty() {
        return Err("歌单不能为空".to_string());
    }
    reject_flag_prefix("歌单 id", playlist)?;
    if let Some(d) = &db {
        let t = d.trim();
        if !t.is_empty() {
            reject_flag_prefix("数据库路径", t)?;
        }
    }
    let mut args: Vec<String> = vec!["--playlist".into(), playlist.to_string(), "--export-songs".into()];
    if let Some(d) = db {
        if !d.trim().is_empty() {
            args.push("--db".into());
            args.push(d.trim().into());
        }
    }
    let (program, prefix) = resolve_engine(python.as_ref());
    let mut cmd = Command::new(&program);
    cmd.args(&prefix).args(&args);
    let out = match run_command_output(&mut cmd, 60) {
        Ok(Some(o)) => o,
        Ok(None) => {
            return Err("导出歌单超时（60s），请检查 python 与数据库后重试".to_string());
        }
        Err(e) => return Err(e),
    };
    let text = decode_line(&out.stdout);
    if !out.status.success() {
        let msg = if text.contains("未找到") {
            "未找到落雪音乐数据库或歌单，请先导入/确认歌单。".to_string()
        } else {
            text
        };
        return Err(msg);
    }
    Ok(text
        .lines()
        .map(|l| l.trim().to_string())
        .filter(|l| !l.is_empty())
        .collect())
}

/// 把文本以 UTF-8 写入用户选择的文件（配合前端保存对话框“另存为”）。
///
/// `path` 来自 dialog 插件（用户自己选的），不是音源脚本可控的输入，
/// 因此不做目录白名单限制；但仍做纵深防御：
///   - 拒绝空路径、目录
///   - 限制扩展名（本命令只用于导出歌单清单）
///   - 限制内容大小，避免一次 IPC 把内存打满
///
/// 用 async + spawn_blocking：目标可能是 U 盘 / 网络盘 / 被占用文件，
/// 同步写可能卡住事件循环（表现为「点保存后界面卡住」）。
#[tauri::command]
async fn save_text_file(path: String, content: String) -> Result<(), String> {
    if path.trim().is_empty() {
        return Err("保存路径为空".to_string());
    }
    const MAX_TEXT_BYTES: usize = 32 * 1024 * 1024; // 32MB 足够放几十万首歌单
    if content.len() > MAX_TEXT_BYTES {
        return Err("导出内容过大（超过 32MB）".to_string());
    }
    let target = std::path::PathBuf::from(&path);
    if target.is_dir() {
        return Err("保存路径是一个目录".to_string());
    }
    // 本命令只用于导出清单，限制为文本扩展名，避免被当成通用写文件原语
    let ext = target
        .extension()
        .map(|e| e.to_string_lossy().to_lowercase())
        .unwrap_or_default();
    if !matches!(ext.as_str(), "txt" | "csv" | "json" | "m3u" | "m3u8" | "log") {
        return Err(format!("不支持的导出文件类型: .{ext}（请用 .txt/.csv/.json/.m3u）"));
    }
    tauri::async_runtime::spawn_blocking(move || {
        std::fs::write(&target, content.as_bytes()).map_err(|e| format!("写入文件失败: {e}"))
    })
    .await
    .map_err(|e| format!("保存任务执行失败: {e}"))?
}

/// 单曲搜索结果（与引擎 --search 的 JSON 字段对应）。
#[derive(Serialize, Clone, serde::Deserialize)]
struct SearchSong {
    name: String,
    #[serde(default)]
    singer: String,
    #[serde(default)]
    source: String,
    #[serde(default)]
    interval: Option<f64>,
    #[serde(default)]
    id: String,
    #[serde(default)]
    songmid: Option<String>,
    #[serde(default)]
    via: String,
    #[serde(default)]
    album: String,
    #[serde(default)]
    qualitys: Vec<String>,
}

/// 歌单内歌曲（与引擎 --list-songs 的 JSON 字段对应）。
///
/// `meta` 必须原样保留：数据库里的 id 是带平台前缀的（如 `wy_30070747`），
/// 音源真正需要的是 meta 里的纯数字 songId。丢掉 meta 会让单曲下载取链失败。
#[derive(Serialize, Clone, serde::Deserialize)]
struct SongItem {
    name: String,
    #[serde(default)]
    singer: String,
    #[serde(default)]
    source: String,
    #[serde(default)]
    interval: String,
    #[serde(default)]
    id: String,
    #[serde(default)]
    meta: serde_json::Value,
}

/// 单曲搜索：按关键词搜索歌曲，返回候选列表（不下载）。
/// 最多阻塞 90s（等引擎 + 网络），改 async + spawn_blocking 避免冻结界面。
#[tauri::command]
async fn search_songs(
    keyword: String,
    limit: Option<usize>,
    python: Option<String>,
) -> Result<Vec<SearchSong>, String> {
    tauri::async_runtime::spawn_blocking(move || search_songs_blocking(keyword, limit, python))
        .await
        .map_err(|e| format!("任务执行失败: {e}"))?
}

fn search_songs_blocking(
    keyword: String,
    limit: Option<usize>,
    python: Option<String>,
) -> Result<Vec<SearchSong>, String> {
    validate_python(python.as_ref())?;
    let kw = keyword.trim();
    if kw.is_empty() {
        return Err("搜索关键词不能为空".to_string());
    }
    reject_flag_prefix("搜索关键词", kw)?;
    let n = limit.unwrap_or(30).clamp(1, 100);
    let args = vec![
        "--search".to_string(),
        kw.to_string(),
        "--search-limit".to_string(),
        n.to_string(),
    ];
    let text = run_engine_json(python.as_ref(), args, 90, "搜索")?;
    let list: Vec<SearchSong> =
        serde_json::from_str(&text).map_err(|e| format!("解析搜索结果失败: {e}"))?;
    Ok(list)
}

/// 列出指定歌单的全部歌曲（GUI 歌单页展开用）。
/// 最多阻塞 60s，改 async + spawn_blocking 避免冻结界面。
#[tauri::command]
async fn list_songs(
    playlist: String,
    db: Option<String>,
    python: Option<String>,
) -> Result<Vec<SongItem>, String> {
    tauri::async_runtime::spawn_blocking(move || list_songs_blocking(playlist, db, python))
        .await
        .map_err(|e| format!("任务执行失败: {e}"))?
}

fn list_songs_blocking(
    playlist: String,
    db: Option<String>,
    python: Option<String>,
) -> Result<Vec<SongItem>, String> {
    validate_python(python.as_ref())?;
    let pl = playlist.trim();
    if pl.is_empty() {
        return Err("歌单不能为空".to_string());
    }
    reject_flag_prefix("歌单 id", pl)?;
    let mut args = vec![
        "--playlist".to_string(),
        pl.to_string(),
        "--list-songs".to_string(),
    ];
    if let Some(d) = &db {
        let t = d.trim();
        if !t.is_empty() {
            reject_flag_prefix("数据库路径", t)?;
            args.push("--db".to_string());
            args.push(t.to_string());
        }
    }
    let text = run_engine_json(python.as_ref(), args, 60, "读取歌单歌曲")?;
    let list: Vec<SongItem> =
        serde_json::from_str(&text).map_err(|e| format!("解析歌曲列表失败: {e}"))?;
    Ok(list)
}

/// 单曲/多曲下载：把选中的歌曲写入临时 JSON，交给引擎的 --song-file 模式下载。
/// 走与歌单下载完全相同的管线（取链 → 四重校验 → 落盘 → 失败重试）。
/// 参数较多是 Tauri 命令的固有形态（前端按名字逐个传），不宜合并成结构体。
///
/// 必须 async：`resolve_engine` 首次运行会同步解压 41MB 引擎包，不能阻塞主线程。
#[allow(clippy::too_many_arguments)]
#[tauri::command]
async fn download_songs(
    app: AppHandle,
    songs: Vec<serde_json::Value>,
    quality: String,
    dir: Option<String>,
    workers: usize,
    fallback: bool,
    min_quality: Option<String>,
    python: Option<String>,
) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        download_songs_blocking(app, songs, quality, dir, workers, fallback, min_quality, python)
    })
    .await
    .map_err(|e| format!("任务执行失败: {e}"))?
}

#[allow(clippy::too_many_arguments)]
fn download_songs_blocking(
    app: AppHandle,
    songs: Vec<serde_json::Value>,
    quality: String,
    dir: Option<String>,
    workers: usize,
    fallback: bool,
    min_quality: Option<String>,
    python: Option<String>,
) -> Result<String, String> {
    validate_python(python.as_ref())?;
    validate_quality(&quality)?;
    if songs.is_empty() {
        return Err("请先选择要下载的歌曲".to_string());
    }
    for (label, v) in [
        ("下载目录", dir.as_deref().unwrap_or("").trim()),
        ("min_quality", min_quality.as_deref().unwrap_or("").trim()),
    ] {
        if !v.is_empty() {
            reject_flag_prefix(label, v)?;
        }
        if label == "min_quality" {
            validate_quality(v)?;
        }
    }

    // 写入临时歌曲文件（引擎读取后自行解析）
    let tmp = std::env::temp_dir().join(format!(
        "lx-download-songs-{}.json",
        std::process::id()
    ));
    let payload = serde_json::json!({ "songs": songs });
    std::fs::write(&tmp, serde_json::to_vec(&payload).unwrap_or_default())
        .map_err(|e| format!("写入歌曲列表失败: {e}"))?;

    let mut args = vec![
        "--song-file".to_string(),
        tmp.to_string_lossy().to_string(),
        "--quality".to_string(),
        quality,
    ];
    if let Some(d) = dir {
        if !d.trim().is_empty() {
            args.push("--dir".to_string());
            args.push(d.trim().to_string());
        }
    }
    if workers > 0 {
        args.push("--workers".to_string());
        args.push(workers.to_string());
    }
    if fallback {
        args.push("--fallback-quality".to_string());
        if let Some(mq) = min_quality {
            if !mq.trim().is_empty() {
                args.push("--min-quality".to_string());
                args.push(mq.trim().to_string());
            }
        }
    }
    let engine = resolve_engine(python.as_ref());
    let state = app.state::<TaskState>();
    spawn_task(app.clone(), &state, "download-log", args, engine)
}

/// 校验失败清单目录参数（`--retry-failed` / `--count-failed` 的值直接进 argv）。
///
/// 返回 `None` 表示「用户没指定目录」——此时**不传值**，由引擎退回它自己的
/// 默认下载目录（与「开始下载」留空时的落点完全一致）。这是主路径：
/// 绝大多数用户不填下载目录，失败清单就在默认目录里。
fn validate_failed_dir(dir: Option<&String>) -> Result<Option<String>, String> {
    let d = dir.map(|s| s.trim()).unwrap_or("");
    if d.is_empty() {
        return Ok(None);
    }
    reject_flag_prefix("下载目录", d)?;
    Ok(Some(d.to_string()))
}

/// 更新检查结果（与引擎 --check-update 的 JSON 对应）。
#[derive(Serialize, Clone, serde::Deserialize)]
struct UpdateInfo {
    #[serde(default)]
    ok: bool,
    #[serde(default)]
    latest: String,
    #[serde(default)]
    name: String,
    #[serde(default)]
    notes: String,
    #[serde(default, rename = "pageUrl")]
    page_url: String,
    #[serde(default, rename = "downloadUrl")]
    download_url: String,
    #[serde(default, rename = "assetName")]
    asset_name: String,
    #[serde(default, rename = "assetSize")]
    asset_size: u64,
    #[serde(default, rename = "publishedAt")]
    published_at: String,
    #[serde(default)]
    error: String,
}

/// 当前应用版本（来自 tauri.conf.json，编译期确定）。
#[tauri::command]
fn app_version(app: AppHandle) -> String {
    app.package_info().version.to_string()
}

/// 检查 GitHub 上是否有新版本。
///
/// 走 Node 而不是 Rust：本项目没有引入 HTTP 客户端依赖，而 Node 已在运行时里。
/// 必须 async + spawn_blocking（要联网，同步会卡界面）。
#[tauri::command]
async fn check_update(python: Option<String>) -> Result<UpdateInfo, String> {
    tauri::async_runtime::spawn_blocking(move || {
        validate_python(python.as_ref())?;
        let text = run_engine_json(python.as_ref(), vec!["--check-update".to_string()], 90,
                                   "检查更新")?;
        serde_json::from_str::<UpdateInfo>(&text)
            .map_err(|e| format!("解析更新信息失败: {e}"))
    })
    .await
    .map_err(|e| format!("任务执行失败: {e}"))?
}

/// 下载更新包到用户指定路径（由前端用系统对话框选位置）。
///
/// 只做「下载 + 落盘」，不自动替换自身——正在运行的程序无法安全覆盖自己的 exe，
/// 由用户手动关闭后替换。这样也避免自动更新出错时把程序搞坏。
#[tauri::command]
async fn download_update(url: String, path: String) -> Result<String, String> {
    if !url.starts_with("https://") {
        return Err("更新包地址必须是 https".to_string());
    }
    if path.trim().is_empty() {
        return Err("保存路径为空".to_string());
    }
    tauri::async_runtime::spawn_blocking(move || {
        // 用系统自带 curl.exe 下载：Windows 10+ 必带，支持 https 与重定向，
        // 无需为一次性功能引入 HTTP 依赖。
        let mut cmd = std::process::Command::new("curl.exe");
        cmd.args(["-L", "--fail", "--silent", "--show-error",
                  "-o", &path, &url]);
        // 后台静默运行，不弹命令提示符窗口
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x08000000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }
        let out = cmd.output().map_err(|e| format!("调用 curl 失败: {e}"))?;
        if !out.status.success() {
            let err = String::from_utf8_lossy(&out.stderr);
            return Err(format!("下载失败: {}", err.trim()));
        }
        let size = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
        if size < 1024 * 1024 {
            return Err(format!("下载的文件过小（{size} 字节），可能不是有效安装包"));
        }
        Ok(path)
    })
    .await
    .map_err(|e| format!("下载任务执行失败: {e}"))?
}

/// 音源体检结果（与引擎 --probe-sources 的 JSON 对应）。
#[derive(Serialize, Clone, serde::Deserialize)]
struct SourceProbe {
    name: String,
    /// ok / flaky / dead
    status: String,
    #[serde(default, rename = "okCount")]
    ok_count: usize,
    #[serde(default)]
    total: usize,
    /// 响应耗时（毫秒），没有成功样本时为 null
    #[serde(default, rename = "speedMs")]
    speed_ms: Option<u64>,
    #[serde(default)]
    error: String,
}

#[derive(Serialize, Clone, serde::Deserialize)]
struct ProbeReport {
    sources: Vec<SourceProbe>,
    #[serde(rename = "probedAt")]
    probed_at: u64,
}

/// 体检结果缓存文件名（放在音源目录，与 .order.json / .health.json 同级）。
fn probe_cache_file() -> std::path::PathBuf {
    user_sources_dir().join(".probe.json")
}

/// 音源体检：逐个实测能否取到可下载链接 + 测响应速度。
///
/// 用 spawn_task 走**流式日志**，而不是 run_engine_json：
/// 体检要跑 20-120 秒，用户必须能实时看到「正在测哪个源」，
/// 否则会以为程序卡死。引擎把进度写 stderr，spawn_task 的日志线程
/// 按行推成 download-log 事件，界面日志区就能实时显示。
///
/// 结果不通过返回值传递（spawn_task 是流式的），而是：
///   引擎跑完后自己把结果写进 `.probe.json`，
///   前端在收到 done 事件后调用 `load_probe_cache` 读取。
#[tauri::command]
async fn probe_sources(app: AppHandle, python: Option<String>) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        validate_python(python.as_ref())?;
        // 先删旧缓存：避免「本次体检失败、界面却显示上次结果」造成误解
        let _ = std::fs::remove_file(probe_cache_file());
        let args = vec!["--probe-sources".to_string()];
        let engine = resolve_engine(python.as_ref());
        let state = app.state::<TaskState>();
        spawn_task(app.clone(), &state, "download-log", args, engine)
    })
    .await
    .map_err(|e| format!("任务执行失败: {e}"))?
}

/// 读取上次体检的缓存结果；没有缓存时返回空列表。
///
/// 体检本身是流式跑的（为了实时日志），结果由引擎写进 `.probe.json`，
/// 这个命令只负责读出来给界面用。
#[tauri::command]
fn load_probe_cache() -> Option<ProbeReport> {
    let raw = std::fs::read_to_string(probe_cache_file()).ok()?;
    serde_json::from_str::<ProbeReport>(&raw).ok()
}

/// 删除指定音源文件（体检后用户选择清理失活音源时调用）。
///
/// 与 remove_source 的区别：支持一次删多个，并且**只删真正存在的**，
/// 返回实际删除的文件名，让前端能准确提示「删了 N 个」。
#[tauri::command]
async fn remove_sources(names: Vec<String>) -> Result<Vec<String>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let dir = user_sources_dir();
        let mut removed = Vec::new();
        for raw in names {
            // 复用单文件校验：剥掉路径、限定 .js/.json
            let name = match validate_source_name(&raw) {
                Ok(n) => n,
                Err(_) => continue,
            };
            let target = dir.join(&name);
            if target.is_file() && std::fs::remove_file(&target).is_ok() {
                removed.push(name);
            }
        }
        Ok::<Vec<String>, String>(removed)
    })
    .await
    .map_err(|e| format!("删除任务执行失败: {e}"))?
}

/// 统计下载目录 failed.txt 里的失败歌曲数，供前端决定是否显示「重试」按钮。
///
/// 必须 async + spawn_blocking：`resolve_engine` 首次运行会同步解压 41MB 引擎包，
/// 同步命令跑在主线程会冻结界面（本项目踩过的坑）。
#[tauri::command]
async fn count_failed(dir: Option<String>, python: Option<String>) -> Result<usize, String> {
    tauri::async_runtime::spawn_blocking(move || count_failed_blocking(dir, python))
        .await
        .map_err(|e| format!("任务执行失败: {e}"))?
}

fn count_failed_blocking(dir: Option<String>, python: Option<String>) -> Result<usize, String> {
    validate_python(python.as_ref())?;
    let mut args = vec!["--count-failed".to_string()];
    if let Some(d) = validate_failed_dir(dir.as_ref())? {
        args.push(d);
    }
    // 超时给 60s：首次运行需要解压引擎包，之后是毫秒级
    let text = run_engine_json(python.as_ref(), args, 60, "统计失败歌曲")?;
    let value: serde_json::Value =
        serde_json::from_str(&text).map_err(|e| format!("解析失败歌曲数量失败: {e}"))?;
    // 引擎返回 {"count":N,"dir":"..."}；字段缺失按 0 处理（没有清单是正常状态）
    Ok(value.get("count").and_then(|v| v.as_u64()).unwrap_or(0) as usize)
}

/// 只重试上次失败的歌：调用引擎的 `--retry-failed`，下载日志走同一个 "download-log" 事件。
///
/// 与 start_download 一样必须 async + spawn_blocking，否则会阻塞主线程。
#[tauri::command]
async fn retry_failed(
    app: AppHandle,
    dir: Option<String>,
    quality: String,
    workers: usize,
    fallback: bool,
    min_quality: Option<String>,
    python: Option<String>,
) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        retry_failed_blocking(app, dir, quality, workers, fallback, min_quality, python)
    })
    .await
    .map_err(|e| format!("任务执行失败: {e}"))?
}

fn retry_failed_blocking(
    app: AppHandle,
    dir: Option<String>,
    quality: String,
    workers: usize,
    fallback: bool,
    min_quality: Option<String>,
    python: Option<String>,
) -> Result<String, String> {
    validate_python(python.as_ref())?;
    // quality 直接进 argv，必须做值域白名单（仅挡 `-` 前缀不足以防 `flac --dir X`）
    validate_quality(&quality)?;
    let dir_value = validate_failed_dir(dir.as_ref())?;
    if let Some(mq) = min_quality.as_deref().map(|s| s.trim()) {
        if !mq.is_empty() {
            reject_flag_prefix("min_quality", mq)?;
            validate_quality(mq)?;
        }
    }

    let mut args = vec!["--retry-failed".to_string()];
    if let Some(d) = &dir_value {
        // 显式传 --dir：让下载落点与失败清单目录一致。
        // 引擎侧也有兜底（未显式给 --dir 时取清单目录），两边都设防。
        args.push(d.clone());
        args.push("--dir".to_string());
        args.push(d.clone());
    }
    args.push("--quality".to_string());
    args.push(quality);
    if workers > 0 {
        args.push("--workers".to_string());
        args.push(workers.to_string());
    }
    if fallback {
        args.push("--fallback-quality".to_string());
        if let Some(mq) = min_quality {
            if !mq.trim().is_empty() {
                args.push("--min-quality".to_string());
                args.push(mq.trim().to_string());
            }
        }
    }
    let engine = resolve_engine(python.as_ref());
    let state = app.state::<TaskState>();
    spawn_task(app.clone(), &state, "download-log", args, engine)
}

/// 运行引擎并解析其 JSON 输出（取最后一行非空输出），供 search_songs / list_songs 复用。
fn run_engine_json(
    python: Option<&String>,
    args: Vec<String>,
    timeout_secs: u64,
    action: &str,
) -> Result<String, String> {
    let (program, prefix) = resolve_engine(python);
    let mut cmd = Command::new(&program);
    cmd.args(&prefix).args(&args);
    let out = match run_command_output(&mut cmd, timeout_secs) {
        Ok(Some(o)) => o,
        Ok(None) => return Err(format!("{action}超时（{timeout_secs}s），请稍后重试")),
        Err(e) => return Err(e),
    };
    let text = decode_line(&out.stdout);
    let stderr_text = decode_line(&out.stderr);
    if !out.status.success() {
        let msg = if text.trim().is_empty() { stderr_text } else { text };
        return Err(if msg.trim().is_empty() {
            format!("{action}失败")
        } else {
            msg.trim().to_string()
        });
    }
    // 引擎可能先打印提示行，JSON 一定在最后一行
    let line = text
        .lines()
        .rev()
        .map(|l| l.trim())
        .find(|l| !l.is_empty())
        .ok_or_else(|| format!("{action}没有返回结果"))?;
    if !line.starts_with('[') && !line.starts_with('{') {
        return Err(format!("{action}返回了非 JSON 内容: {}", line.chars().take(120).collect::<String>()));
    }
    Ok(line.to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(TaskState::default())
        .invoke_handler(tauri::generate_handler![
            list_playlists,
            start_download,
            stop_task,
            get_sources_dir,
            list_sources,
            import_source,
            remove_source,
            is_task_running,
            get_source_order,
            set_source_order,
            find_lx_desktop_api,
            import_lx_desktop_sources,
            search_songs,
            list_songs,
            download_songs,
            retry_failed,
            count_failed,
            probe_sources,
            load_probe_cache,
            remove_sources,
            app_version,
            check_update,
            download_update,
            export_songs,
            save_text_file
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::*;

    // ===== 命令行注入防护 =====

    #[test]
    fn reject_flag_prefix_blocks_dash() {
        assert!(reject_flag_prefix("歌单", "-rf").is_err());
        assert!(reject_flag_prefix("歌单", "  --db").is_err());
    }

    #[test]
    fn reject_flag_prefix_allows_normal_values() {
        assert!(reject_flag_prefix("歌单", "love").is_ok());
        assert!(reject_flag_prefix("目录", r"D:\Music").is_ok());
        assert!(reject_flag_prefix("歌单", "").is_ok());
    }

    // ===== 音质白名单（argv 注入防护）=====

    #[test]
    fn validate_quality_accepts_known_tiers() {
        for q in ["128k", "192k", "320k", "flac", "flac24bit"] {
            assert!(validate_quality(q).is_ok(), "{q}");
        }
        // 空值代表「用引擎默认值」
        assert!(validate_quality("").is_ok());
    }

    #[test]
    fn validate_quality_rejects_flag_injection() {
        // 这是本项目最重要的一条注入防护：只挡 `-` 前缀不足以防住这种值，
        // 因为它不以 `-` 开头，却会被 argparse 拆成 "--dir <任意目录>"。
        assert!(validate_quality("flac --dir C:\\Windows\\System32").is_err());
        assert!(validate_quality("flac --db D:\\evil.db").is_err());
        assert!(validate_quality("320k extra").is_err());
        assert!(validate_quality("--quality").is_err());
    }

    #[test]
    fn validate_quality_rejects_unknown() {
        assert!(validate_quality("999k").is_err());
        assert!(validate_quality("flac ").is_ok(), "首尾空白应被容忍");
    }

    // ===== 失败清单目录（--retry-failed / --count-failed 的 argv 值）=====

    #[test]
    fn validate_failed_dir_accepts_normal_paths() {
        assert_eq!(
            validate_failed_dir(Some(&r"D:\Music".to_string())).unwrap(),
            Some(r"D:\Music".to_string())
        );
        // 首尾空白应被裁掉，否则引擎会把带空白的路径当字面目录名
        assert_eq!(
            validate_failed_dir(Some(&"  D:\\Music  ".to_string())).unwrap(),
            Some(r"D:\Music".to_string())
        );
    }

    #[test]
    fn validate_failed_dir_rejects_flag_injection() {
        // 目录值直接进 argv，以 - 开头会被 argparse 当成另一个选项
        assert!(validate_failed_dir(Some(&"--dir D:\\evil".to_string())).is_err());
        assert!(validate_failed_dir(Some(&"-rf".to_string())).is_err());
    }

    #[test]
    fn validate_failed_dir_empty_means_engine_default() {
        // 空目录不是错误：绝大多数用户不填下载目录，此时不传值，
        // 由引擎退回它自己的默认目录（与「开始下载」留空时落点一致）。
        assert_eq!(validate_failed_dir(None).unwrap(), None);
        assert_eq!(validate_failed_dir(Some(&"   ".to_string())).unwrap(), None);
    }

    // ===== python 路径白名单 =====

    #[test]
    fn validate_python_accepts_empty() {
        assert!(validate_python(None).is_ok());
        assert!(validate_python(Some(&"   ".to_string())).is_ok());
    }
    #[test]
    fn validate_python_rejects_missing_file() {
        assert!(validate_python(Some(&r"C:\nope\python.exe".to_string())).is_err());
    }

    #[test]
    fn validate_python_rejects_non_interpreter() {
        // 存在的文件但不是解释器名 → 拒绝（防止任意命令执行）
        let exe = std::env::current_exe().unwrap();
        let path = exe.to_string_lossy().to_string();
        assert!(validate_python(Some(&path)).is_err());
    }

    // ===== 音源文件名校验（目录穿越防护） =====

    #[test]
    fn validate_source_name_strips_paths() {
        assert_eq!(validate_source_name(r"C:\evil\x.js").unwrap(), "x.js");
        assert_eq!(validate_source_name("../../x.json").unwrap(), "x.json");
    }

    #[test]
    fn validate_source_name_rejects_bad_ext() {
        assert!(validate_source_name("evil.exe").is_err());
        assert!(validate_source_name("noext").is_err());
        assert!(validate_source_name("..").is_err());
        assert!(validate_source_name("").is_err());
    }

    #[test]
    fn validate_source_name_accepts_js_and_json() {
        assert!(validate_source_name("音源 v1.2.js").is_ok());
        assert!(validate_source_name("user_api.JSON").is_ok());
    }

    // ===== 元数据文件排除（.order.json / .health.json 不是音源）=====

    #[test]
    fn is_meta_file_excludes_program_metadata() {
        assert!(is_meta_file(".order.json"));
        assert!(is_meta_file(".health.json"));
    }

    #[test]
    fn is_meta_file_keeps_real_sources() {
        // 真实的音源文件名（含以点开头的合法名）不能被误排除
        for name in ["六音音源.js", "野草🌾.js", "user_api.json", "音源 v1.2.js"] {
            assert!(!is_meta_file(name), "{name} 不应被当成元数据文件");
        }
    }

    // ===== 歌单解析 =====

    #[test]
    fn parse_playlists_reads_engine_output() {
        let out = "落雪音乐中的歌单：\n  [default] 默认列表 (531 首)\n  [love] 我的收藏 (227 首)\n";
        let pls = parse_playlists(out).unwrap();
        assert_eq!(pls.len(), 2);
        assert_eq!(pls[0].id, "default");
        assert_eq!(pls[0].name, "默认列表");
        assert_eq!(pls[0].cnt, 531);
        assert_eq!(pls[1].id, "love");
        assert_eq!(pls[1].cnt, 227);
    }

    #[test]
    fn parse_playlists_handles_names_with_spaces() {
        let out = "  [userlist_1] 我的 私人 歌单 (12 首)\n";
        let pls = parse_playlists(out).unwrap();
        assert_eq!(pls[0].name, "我的 私人 歌单");
        assert_eq!(pls[0].cnt, 12);
    }

    #[test]
    fn parse_playlists_errors_when_empty() {
        assert!(parse_playlists("no playlists here").is_err());
    }

    // ===== 日志解码 / 归类 =====

    #[test]
    fn decode_line_prefers_utf8() {
        assert_eq!(decode_line("晴天 - 周杰伦".as_bytes()), "晴天 - 周杰伦");
    }

    #[test]
    fn decode_line_falls_back_to_gbk() {
        // GBK 编码的“中文”，不是合法 UTF-8
        let (gbk, _, _) = encoding_rs::GBK.encode("中文");
        assert_eq!(decode_line(&gbk), "中文");
    }

    #[test]
    fn classify_log_uses_line_prefix() {
        assert_eq!(classify_log("[错误] 失败"), "error");
        assert_eq!(classify_log("[失败] 音质不达标"), "error");
        assert_eq!(classify_log("  [致命错误] x"), "error");
        assert_eq!(classify_log("[成功] 晴天 [flac]"), "ok");
        assert_eq!(classify_log("[转换完成] x"), "ok");
        assert_eq!(classify_log("[跳过] 已存在"), "info");
        // 关键字出现在行中不应被误判为错误
        assert_eq!(classify_log("下载 [错误] 已忽略"), "info");
    }

    #[test]
    fn trim_end_strips_crlf() {
        assert_eq!(trim_end(b"line\r\n"), b"line".to_vec());
        assert_eq!(trim_end(b"line\n\n"), b"line".to_vec());
        assert_eq!(trim_end(b"line"), b"line".to_vec());
    }

    // ===== 任务状态机 =====

    #[test]
    fn task_state_default_is_idle() {
        let st = TaskState::default();
        assert!(!st
            .starting
            .load(std::sync::atomic::Ordering::SeqCst));
        assert!(st.child.lock().unwrap().is_none());
        assert!(st.stop_file.lock().unwrap().is_none());
    }

    #[test]
    fn starting_flag_toggles() {
        let st = TaskState::default();
        st.starting
            .store(true, std::sync::atomic::Ordering::SeqCst);
        assert!(st.starting.load(std::sync::atomic::Ordering::SeqCst));
        st.starting
            .store(false, std::sync::atomic::Ordering::SeqCst);
        assert!(!st.starting.load(std::sync::atomic::Ordering::SeqCst));
    }

    #[test]
    fn child_arc_is_shared_not_moved() {
        // 确认 child 是 Arc：clone 后仍是同一个底层锁，
        // 这样 stop 后台线程与 Drop 能共享同一个 child。
        let st = TaskState::default();
        let a = st.child.clone();
        let b = st.child.clone();
        assert!(Arc::ptr_eq(&a, &b));
    }

    // ===== 引擎版本标记 =====

    #[test]
    fn engine_version_is_stable_and_hex() {
        let a = engine_version();
        let b = engine_version();
        assert_eq!(a, b, "同一份内嵌 zip 必须得到相同版本号");
        assert_eq!(a.len(), 64, "SHA-256 十六进制应为 64 字符");
        assert!(a.chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn engine_version_changes_with_content() {
        // 长度相同、内容不同也必须得到不同版本（这是改用哈希的原因）
        let mut hasher = <sha2::Sha256 as sha2::Digest>::new();
        sha2::Digest::update(&mut hasher, b"aaaa");
        let h1 = format!("{:x}", sha2::Digest::finalize(hasher));
        let mut hasher = <sha2::Sha256 as sha2::Digest>::new();
        sha2::Digest::update(&mut hasher, b"bbbb");
        let h2 = format!("{:x}", sha2::Digest::finalize(hasher));
        assert_ne!(h1, h2);
        assert_eq!(h1.len(), h2.len());
    }

    // ===== 落雪音源导入 =====

    #[test]
    fn decode_lx_script_plain_js() {
        let js = "lx.on('request', () => {})";
        assert_eq!(decode_lx_script(js).unwrap(), js);
    }

    #[test]
    fn decode_lx_script_gz_prefix() {
        // 构造 gz_ = base64(zlib(deflate(js))) 并验证能还原
        use base64::Engine;
        use std::io::Write;
        let js = "const a = 1;";
        let mut enc = flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::default());
        enc.write_all(js.as_bytes()).unwrap();
        let compressed = enc.finish().unwrap();
        let b64 = base64::engine::general_purpose::STANDARD.encode(&compressed);
        let decoded = decode_lx_script(&format!("gz_{b64}")).unwrap();
        assert_eq!(decoded, js);
    }

    #[test]
    fn decode_lx_script_rejects_bad_base64() {
        assert!(decode_lx_script("gz_!!!not-base64!!!").is_err());
    }

    #[test]
    fn sanitize_file_stem_strips_illegal_chars() {
        assert_eq!(sanitize_file_stem("a/b:c*d?e\"f<g>h|i"), "a_b_c_d_e_f_g_h_i");
        assert_eq!(sanitize_file_stem("音源 v1.2"), "音源 v1.2");
    }

    #[test]
    fn sanitize_file_stem_handles_empty_and_dots() {
        assert_eq!(sanitize_file_stem(""), "lx-source");
        assert_eq!(sanitize_file_stem("   "), "lx-source");
        assert_eq!(sanitize_file_stem("..."), "lx-source");
    }

    #[test]
    fn sanitize_file_stem_truncates_long_names() {
        let long = "音".repeat(200);
        let out = sanitize_file_stem(&long);
        assert_eq!(out.chars().count(), 80);
    }

    #[test]
    fn lx_api_candidates_cover_known_layouts() {
        let c = lx_desktop_api_candidates();
        assert!(!c.is_empty());
        assert!(c.iter().any(|p| p.to_string_lossy().contains("lx-music-desktop")));
        assert!(c.iter().any(|p| p.to_string_lossy().ends_with("user_api.json")));
    }

    // ===== 引擎 JSON 输出解析 =====

    #[test]
    fn search_song_deserializes() {
        let json = r#"[{"name":"晴天","singer":"周杰伦","source":"kw","interval":269,
                       "id":"51685512","songmid":"51685512","via":"酷我搜索",
                       "album":"","qualitys":[]}]"#;
        let list: Vec<SearchSong> = serde_json::from_str(json).unwrap();
        assert_eq!(list.len(), 1);
        assert_eq!(list[0].name, "晴天");
        assert_eq!(list[0].interval, Some(269.0));
        assert_eq!(list[0].id, "51685512");
    }

    #[test]
    fn search_song_tolerates_missing_optional_fields() {
        let json = r#"[{"name":"A","id":"1"}]"#;
        let list: Vec<SearchSong> = serde_json::from_str(json).unwrap();
        assert_eq!(list[0].singer, "");
        assert_eq!(list[0].source, "");
        assert_eq!(list[0].qualitys.len(), 0);
    }

    #[test]
    fn song_item_deserializes() {
        let json = r#"[{"name":"晴天","singer":"周杰伦","source":"kw",
                       "interval":"04:29","id":"kw_1"}]"#;
        let list: Vec<SongItem> = serde_json::from_str(json).unwrap();
        assert_eq!(list[0].interval, "04:29");
    }
}