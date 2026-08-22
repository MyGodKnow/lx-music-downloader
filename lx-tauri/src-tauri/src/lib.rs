use serde::Serialize;
use std::io::{BufReader, Read};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use tauri::{AppHandle, Emitter, State};

/// 全局状态：当前正在运行的子进程（下载或转换），每次只允许一个。
#[derive(Default)]
struct TaskState {
    child: Mutex<Option<Child>>,
}

/// App 退出（窗口关闭）时也清理仍在运行的子进程树，避免遗留 python/node/ffmpeg。
impl Drop for TaskState {
    fn drop(&mut self) {
        if let Ok(mut guard) = self.child.lock() {
            if let Some(mut child) = guard.take() {
                kill_process_tree(&mut child);
            }
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

/// 确保内置引擎已释放到本地；版本变化（内嵌 zip 长度不同）时重新释放。
/// 返回引擎 EXE 的完整路径。
fn ensure_engine() -> Result<std::path::PathBuf, String> {
    let dir = engine_home().ok_or_else(|| "无法定位 LOCALAPPDATA".to_string())?;
    let exe = dir.join("lx_music_downloader.exe");
    let marker = dir.join(".engine_size");
    let ver = ENGINE_ZIP.len().to_string();
    let fresh = exe.is_file()
        && std::fs::read_to_string(&marker)
            .map(|v| v.trim() == ver)
            .unwrap_or(false);
    if fresh {
        return Ok(exe);
    }
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).map_err(|e| format!("创建引擎目录失败: {e}"))?;
    let cursor = std::io::Cursor::new(ENGINE_ZIP);
    let mut archive = zip::ZipArchive::new(cursor).map_err(|e| format!("内置引擎包损坏: {e}"))?;
    archive.extract(&dir).map_err(|e| format!("释放内置引擎失败: {e}"))?;
    std::fs::write(&marker, &ver).map_err(|e| format!("写入引擎版本标记失败: {e}"))?;
    Ok(exe)
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

/// 结束子进程并连带清理其子孙进程（Python 会派生 node/ffmpeg）。
/// Windows 用 taskkill /T 杀整棵进程树，避免停止后遗留孤儿进程。
fn kill_process_tree(child: &mut Child) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        let _ = Command::new("taskkill")
            .args(["/PID", &child.id().to_string(), "/T", "/F"])
            .creation_flags(CREATE_NO_WINDOW)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .output();
    }
    let _ = child.kill();
    let _ = child.wait();
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

/// 组装命令行，启动子进程并起读取线程推日志。
/// engine = (程序, 前置参数)：内置引擎直接执行 EXE；开发模式为 python + 脚本。
fn spawn_task(
    app: AppHandle,
    state: &TaskState,
    event_name: &'static str,
    args: Vec<String>,
    engine: (String, Vec<String>),
) -> Result<String, String> {
    // 先杀掉上一次任务（连带清理其子进程）
    if let Ok(mut guard) = state.child.lock() {
        if let Some(mut old) = guard.take() {
            kill_process_tree(&mut old);
        }
    }

    let mut cmd = Command::new(&engine.0);
    cmd.args(&engine.1).args(&args);
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

    let mut child = cmd.spawn().map_err(|e| format!("启动子进程失败: {e}"))?;
    let stdout = child.stdout.take().expect("no stdout");
    let stderr = child.stderr.take().expect("no stderr");
    if let Ok(mut guard) = state.child.lock() {
        *guard = Some(child);
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
#[tauri::command]
fn list_playlists(db: Option<String>, python: Option<String>) -> Result<Vec<Playlist>, String> {
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
#[tauri::command]
fn start_download(
    _app: AppHandle,
    state: State<'_, TaskState>,
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
    // 统一校验：进入 argv 的空字符串值不得以 `-` 开头
    for (label, v) in [
        ("下载目录", dir.as_deref().unwrap_or("").trim()),
        ("min_quality", min_quality.as_deref().unwrap_or("").trim()),
        ("数据库路径", db.as_deref().unwrap_or("").trim()),
    ] {
        if !v.is_empty() {
            reject_flag_prefix(label, v)?;
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
    spawn_task(_app, &state, "download-log", args, engine)
}

/// 停止当前任务。
#[tauri::command]
fn stop_task(state: State<'_, TaskState>) -> Result<(), String> {
    let mut guard = state.child.lock().map_err(|_| "状态锁失效".to_string())?;
    if let Some(mut child) = guard.take() {
        kill_process_tree(&mut child);
    }
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(TaskState::default())
        .invoke_handler(tauri::generate_handler![
            list_playlists,
            start_download,
            stop_task
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}