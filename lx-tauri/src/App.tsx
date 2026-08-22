import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import "./App.css";

interface Playlist {
  id: string;
  name: string;
  cnt: number;
}

interface LogLine {
  kind: "info" | "ok" | "error" | "done";
  data: string;
}

type Tab = "playlists" | "download" | "settings";

const QUALITY_OPTIONS = [
  { value: "flac24bit", label: "Hi-Res 无损", hint: "FLAC 24bit" },
  { value: "flac", label: "无损", hint: "FLAC" },
  { value: "320k", label: "高品质", hint: "320k MP3" },
  { value: "192k", label: "较高", hint: "192k MP3" },
  { value: "128k", label: "标准", hint: "128k MP3" },
];

interface Progress {
  done: number;
  total: number;
  success: number;
  fail: number;
  skip: number;
  active?: number;
  speed?: number;
}

function App() {
  const [tab, setTab] = useState<Tab>("playlists");
  const [playlists, setPlaylists] = useState<Playlist[]>([]);
  const [loadingPl, setLoadingPl] = useState(false);
  const [dbPath, setDbPath] = useState("");
  const [pythonPath, setPythonPath] = useState("");

  const [quality, setQuality] = useState("flac");
  const [fallback, setFallback] = useState(true);
  const [minQuality, setMinQuality] = useState("320k");
  const [workers, setWorkers] = useState(0);
  const [dir, setDir] = useState("");
  const [selected, setSelected] = useState<string>("");

  const [logs, setLogs] = useState<LogLine[]>([]);
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState<Progress | null>(null);
  const logEndRef = useRef<HTMLDivElement>(null);

  // 首页自动加载歌单
  useEffect(() => {
    loadPlaylists();
  }, []);

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

  // 订阅下载日志
  useEffect(() => {
    let unH: (() => void) | undefined;
    (async () => {
      unH = await listen<LogLine>("download-log", (e) => {
        handleLog(e.payload);
      });
    })();
    return () => {
      unH?.();
    };
  }, []);

  // 订阅进度事件（下载用）
  useEffect(() => {
    let unH: (() => void) | undefined;
    (async () => {
      unH = await listen<string>("progress", (e) => {
        try {
          const p = JSON.parse(e.payload) as Progress;
          setProgress(p);
          if (p.done >= p.total) setRunning(false);
        } catch {
          /* ignore badly-formed progress */
        }
      });
    })();
    return () => {
      unH?.();
    };
  }, []);

  function handleLog(line: LogLine) {
    setLogs((prev) => [...prev, line]);
    if (line.kind === "done") setRunning(false);
  }

  async function loadPlaylists() {
    setLoadingPl(true);
    setLogs([]);
    try {
      const pls = await invoke<Playlist[]>("list_playlists", {
        db: dbPath || null,
        python: pythonPath || null,
      });
      setPlaylists(pls);
      if (pls.length > 0) setSelected(pls[0].id);
      addLog("info", `成功读取 ${pls.length} 个歌单`);
    } catch (err) {
      setPlaylists([]);
      addLog("error", String(err));
    } finally {
      setLoadingPl(false);
    }
  }

  function addLog(kind: LogLine["kind"], data: string) {
    setLogs((prev) => [...prev, { kind, data }]);
  }

  function clearLogs() {
    setLogs([]);
  }

  async function startDownload() {
    if (!selected) {
      addLog("error", "请先选择一个歌单");
      return;
    }
    setRunning(true);
    setProgress({ done: 0, total: 0, success: 0, fail: 0, skip: 0 });
    addLog("info", `开始下载歌单…（音质 ${quality}，${workers > 0 ? workers + " 并发" : "自动并发"}）`);
    try {
      await invoke("start_download", {
        playlist: selected,
        quality,
        dir: dir || null,
        workers,
        fallback,
        minQuality,
        db: dbPath || null,
        python: pythonPath || null,
      });
    } catch (err) {
      addLog("error", String(err));
      setRunning(false);
    }
  }

  async function stop() {
    try {
      await invoke("stop_task");
      addLog("info", "已请求停止当前任务");
      setRunning(false);
    } catch (err) {
      addLog("error", String(err));
    }
  }

  const logCount = logs.filter((l) => l.kind === "ok").length;

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-icon">♫</div>
          <div className="brand-text">
            <h1>落雪下载</h1>
            <span>Lossless · 320k</span>
          </div>
        </div>
        <nav>
          {(
            [
              ["playlists", "我的歌单"],
              ["download", "下载歌曲"],
              ["settings", "设置"],
            ] as [Tab, string][]
          ).map(([key, label]) => (
            <button
              key={key}
              className={`nav-item ${tab === key ? "active" : ""}`}
              onClick={() => setTab(key)}
            >
              <span className="nav-dot" />
              {label}
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <span className={`status-dot ${running ? "pulse" : ""}`} />
          {running ? "任务运行中" : "空闲"}
        </div>
      </aside>

      <main className="content">
        <header className="topbar">
          <h2>{tabTitle(tab)}</h2>
          <div className="topbar-actions">
            {logs.length > 0 && (
              <button className="ghost-btn" onClick={clearLogs}>
                清空日志
              </button>
            )}
            {running && (
              <button className="danger-btn" onClick={stop}>
                停止
              </button>
            )}
          </div>
        </header>

        {progress && (
          <div className="progressbar-wrap">
            <div className="progress-meta">
              <span>
                {progress.total > 0
                  ? `进度 ${progress.done}/${progress.total}`
                  : "准备中…"}
              </span>
              <span className="progress-stats">
                <em className="st-ok">✓ {progress.success}</em>
                <em className="st-fail">✗ {progress.fail}</em>
                <em className="st-skip">跳过 {progress.skip}</em>
                {progress.active != null && progress.active > 0 && (
                  <em className="st-active">· 进行中 {progress.active}</em>
                )}
                {progress.speed != null && <em>· {progress.speed}首/秒</em>}
              </span>
            </div>
            <div className="progressbar">
              <div
                className="progressbar-fill"
                style={{
                  width: `${progress.total > 0 ? Math.min(100, (progress.done / progress.total) * 100) : 0}%`,
                }}
              />
            </div>
          </div>
        )}

        <div className="page">
          {tab === "playlists" && (
            <section className="panel">
              <div className="panel-row">
                <label className="field grow">
                  <span>落雪数据库路径（留空自动探测）</span>
                  <input
                    value={dbPath}
                    onChange={(e) => setDbPath(e.target.value)}
                    placeholder="例如 C:\Users\你\AppData\Roaming\lx-music-desktop\LxDatas\lx.data.db"
                  />
                </label>
                <button className="primary-btn" onClick={loadPlaylists} disabled={loadingPl}>
                  {loadingPl ? "加载中…" : "刷新歌单"}
                </button>
              </div>

              <p className="hint">
                也可以直接填入落雪导出的 <code>.lxmc</code> / <code>.json</code>{" "}
                歌单文件路径读取。
              </p>

              {playlists.length > 0 && (
                <div className="playlist-grid">
                  {playlists.map((p) => (
                    <button
                      key={p.id}
                      className={`playlist-card ${selected === p.id ? "selected" : ""}`}
                      onClick={() => {
                        setSelected(p.id);
                        setTab("download");
                      }}
                    >
                      <div className="pl-art">{p.name.charAt(0)}</div>
                      <div className="pl-info">
                        <div className="pl-name">{p.name}</div>
                        <div className="pl-cnt">{p.cnt} 首</div>
                      </div>
                      <span className="pl-id">{p.id}</span>
                    </button>
                  ))}
                </div>
              )}

              {playlists.length === 0 && !loadingPl && (
                <div className="empty">
                  <div className="empty-icon">🎵</div>
                  <p>暂时没有读取到歌单</p>
                  <p className="hint">请确认落雪音乐桌面版已运行，或手动填入数据库 / 歌单路径</p>
                </div>
              )}

              <LogView logs={logs} logEndRef={logEndRef} />
            </section>
          )}

          {tab === "download" && (
            <section className="panel">
              <div className="panel-row">
                <label className="field grow">
                  <span>目标歌单</span>
                  <select value={selected} onChange={(e) => setSelected(e.target.value)}>
                    {playlists.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}（{p.cnt} 首）
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field grow">
                  <span>下载目录（留空用默认）</span>
                  <input
                    value={dir}
                    onChange={(e) => setDir(e.target.value)}
                    placeholder="例如 D:\Music"
                  />
                </label>
              </div>

              <div className="panel-row">
                <div className="field">
                  <span>音质</span>
                  <div className="quality-grid">
                    {QUALITY_OPTIONS.map((q) => (
                      <button
                        key={q.value}
                        className={`quality-btn ${quality === q.value ? "active" : ""}`}
                        onClick={() => setQuality(q.value)}
                      >
                        <div>{q.label}</div>
                        <small>{q.hint}</small>
                      </button>
                    ))}
                  </div>
                </div>
              </div>

              <div className="panel-row opt-row">
                <label className="checkbox">
                  <input
                    type="checkbox"
                    checked={fallback}
                    onChange={(e) => setFallback(e.target.checked)}
                  />
                  <span>失败时自动降级音质</span>
                </label>
                <label className="field slim">
                  <span>最低降级到</span>
                  <select
                    value={minQuality}
                    onChange={(e) => setMinQuality(e.target.value)}
                    disabled={!fallback}
                  >
                    <option value="320k">320k (高品质)</option>
                    <option value="flac">flac (无损)</option>
                    <option value="flac24bit">flac24bit (Hi-Res)</option>
                  </select>
                </label>
                <label className="field slim">
                  <span>并发数(0=自动)</span>
                  <input
                    type="number"
                    min={0}
                    max={32}
                    value={workers}
                    onChange={(e) => setWorkers(Number(e.target.value))}
                  />
                </label>
              </div>

              <div className="panel-row">
                <button
                  className="primary-btn big"
                  onClick={startDownload}
                  disabled={running || playlists.length === 0}
                >
                  {running ? "正在下载…" : "开始下载"}
                </button>
                {playlists.length === 0 && (
                  <span className="hint">请先在「我的歌单」页加载歌单</span>
                )}
              </div>

              <LogView logs={logs} logEndRef={logEndRef} />
            </section>
          )}

          {tab === "settings" && (
            <section className="panel">
              <div className="settings-grid">
                <label className="field">
                  <span>Python 可执行路径（默认使用系统 python）</span>
                  <input
                    value={pythonPath}
                    onChange={(e) => setPythonPath(e.target.value)}
                    placeholder="例如 C:\Users\你\AppData\Local\Programs\Python\python.exe"
                  />
                </label>
              </div>
              <div className="stats">
                <div className="stat">
                  <div className="stat-num">{playlists.length}</div>
                  <div className="stat-label">已加载歌单</div>
                </div>
                <div className="stat">
                  <div className="stat-num">{logCount}</div>
                  <div className="stat-label">成功条目</div>
                </div>
                <div className="stat">
                  <div className="stat-num">{logs.length}</div>
                  <div className="stat-label">日志行数</div>
                </div>
              </div>
              <p className="hint">
                本应用复用已验证的 Python 下载引擎：多源搜索桥接、下载内容音质校验（无损/320k+）、
                版本词与歌手校验等能力均保留。
              </p>
            </section>
          )}
        </div>
      </main>
    </div>
  );
}

function tabTitle(tab: Tab): string {
  const map: Record<Tab, string> = {
    playlists: "我的歌单",
    download: "下载歌曲",
    settings: "设置",
  };
  return map[tab];
}

function LogView({ logs, logEndRef }: { logs: LogLine[]; logEndRef: React.RefObject<HTMLDivElement | null> }) {
  return (
    <div className="logbox">
      {logs.length === 0 ? (
        <div className="log-empty">运行日志将显示在这里</div>
      ) : (
        logs.map((l, i) => (
          <div key={i} className={`log-line ${l.kind}`}>
            <span className="log-tag">{l.kind}</span>
            <span className="log-text">{l.data}</span>
          </div>
        ))
      )}
      <div ref={logEndRef} />
    </div>
  );
}

export default App;