import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { getVersion } from "@tauri-apps/api/app";
import { listen } from "@tauri-apps/api/event";
import { open as openDialog, save } from "@tauri-apps/plugin-dialog";
import { openUrl } from "@tauri-apps/plugin-opener";
import {
  BrandMark,
  CopyIcon,
  DiscIcon,
  DownloadIcon,
  ExportIcon,
  ExternalLinkIcon,
  FileTextIcon,
  InfoIcon,
  ListIcon,
  PlugIcon,
  SearchIcon,
  SettingsIcon,
} from "./icons";
import "./App.css";

interface Playlist {
  id: string;
  name: string;
  cnt: number;
}

/** 单个音源的体检结果（与 Rust SourceProbe 对应） */
interface SourceProbe {
  name: string;
  /** ok = 全部探测成功；flaky = 部分成功；dead = 一次都没成功 */
  status: "ok" | "flaky" | "dead";
  okCount: number;
  total: number;
  /** 响应耗时中位数（毫秒）；没有成功样本时为 null */
  speedMs: number | null;
  error: string;
}

interface ProbeReport {
  sources: SourceProbe[];
  probedAt: number;
}

/** 更新检查结果（与 Rust UpdateInfo 对应） */
interface UpdateInfo {
  ok: boolean;
  latest: string;
  name: string;
  notes: string;
  pageUrl: string;
  downloadUrl: string;
  assetName: string;
  assetSize: number;
  publishedAt: string;
  error: string;
}

interface LogLine {
  kind: "info" | "ok" | "error" | "done";
  data: string;
}

type Tab =
  | "playlists"
  | "search"
  | "download"
  | "export"
  | "sources"
  | "settings"
  | "about";

const QUALITY_OPTIONS = [
  { value: "flac24bit", label: "Hi-Res 无损", hint: "FLAC 24bit" },
  { value: "flac", label: "无损", hint: "FLAC" },
  { value: "320k", label: "高品质", hint: "320k MP3" },
  { value: "192k", label: "较高", hint: "192k MP3" },
  { value: "128k", label: "标准", hint: "128k MP3" },
];

/** 引擎 --search / --list-songs 返回的歌曲结构 */
interface Song {
  name: string;
  singer: string;
  source: string;
  interval?: number | string | null;
  id: string;
  songmid?: string | null;
  via?: string;
  album?: string;
  qualitys?: string[];
  /** 引擎 --list-songs 带回的原始 meta（含纯数字 songId），单曲下载必须透传 */
  meta?: Record<string, unknown> | null;
}

interface Progress {
  done: number;
  total: number;
  success: number;
  fail: number;
  skip: number;
  active?: number;
  speed?: number;
}

const AUTHOR = {
  name: "MyGodKnow",
  qq: "1973490065",
  email: "114514woguizhidaoya@gmail.com",
  repo: "https://github.com/MyGodKnow/lx-music-downloader",
  issues: "https://github.com/MyGodKnow/lx-music-downloader/issues",
  /** GitHub 点 Star 直达链接（仓库主页即可，Star 按钮在右上角） */
  star: "https://github.com/MyGodKnow/lx-music-downloader",
};

/**
 * 应用版本号。
 *
 * 之前这里写死成 v0.2.0，后来升到 1.x 时忘了改，关于页一直显示旧版本。
 * 现在改为启动时从 Tauri 读取（唯一来源是 tauri.conf.json），
 * 读不到时才退回占位符——避免以后再出现「关于页版本号和实际不符」。
 */

/**
 * 比较两个语义化版本号。
 * 返回 1 表示 a > b，-1 表示 a < b，0 表示相等。
 *
 * 只比较前三段数字（major.minor.patch），忽略 `-beta` 之类的预发布后缀——
 * 对本项目足够，且避免把 "1.1.0-beta" 误判成比 "1.1.0" 新。
 * 解析失败时返回 0（宁可漏提示，也不要误报有新版本）。
 */
function compareVersion(a: string, b: string): number {
  const parse = (v: string): number[] | null => {
    // 允许两段版本（如 "1.0"）与三段版本，"1.0" 等价于 "1.0.0"。
    // 原实现要求必须三段，遇到 "1.0" 会解析成 null → 返回 0 → 漏掉更新提示。
    const m = String(v || "")
      .trim()
      .replace(/^v/i, "")
      .match(/^(\d+)(?:\.(\d+))?(?:\.(\d+))?/);
    if (!m) return null;
    return [Number(m[1] || 0), Number(m[2] || 0), Number(m[3] || 0)];
  };
  const pa = parse(a);
  const pb = parse(b);
  // 任一版本无法解析时返回 0（"无法比较"），由调用方决定提示文案，
  // 不要在这里假装"相等"——那会被上层当成"已是最新"。
  if (!pa || !pb) return 0;
  for (let i = 0; i < 3; i += 1) {
    if (pa[i] > pb[i]) return 1;
    if (pa[i] < pb[i]) return -1;
  }
  return 0;
}

/**
 * 给体检结果算「综合得分」，用于一键按体检结果重排音源优先级。
 *
 * 两个维度，权重刻意拉开：
 *   - 稳定性（主导）：全部探测成功 > 部分成功（flaky）> 全失败（dead）
 *   - 速度（次要）：同等稳定性下，响应快的优先
 *
 * 分数区间：稳定性 ok=1000 / flaky=500 / dead=0，速度最多再加 0~100。
 * 这样「稳定但慢」永远排在「不稳定但快」之前——一个偶尔抽风的源哪怕很快，
 * 中途失败要重试，代价远大于多等几百毫秒。
 */
function scoreProbe(status: SourceProbe["status"], speedMs: number | null, maxSpeed: number): number {
  const stability = status === "ok" ? 1000 : status === "flaky" ? 500 : 0;
  if (stability === 0) return 0;
  if (speedMs == null || maxSpeed <= 0) return stability;
  return stability + Math.max(0, 100 * (1 - speedMs / maxSpeed));
}

/**
 * 按「稳定性 → 速度」生成新的音源优先级顺序。
 *
 * 返回的文件名数组用于写回 .order.json（即用户拖拽用的同一份优先级）。
 * 处理三类源：
 *   1. 体检里存活（ok/flaky）：按得分降序
 *   2. 体检里失活（dead）：排在最后（保留但不建议用；用户可自行删除）
 *   3. 没参与本次体检的（体检后新导入）：保持原有相对顺序，插在存活源之后
 * 这样既落实了排序结果，又不会把用户其它的源弄丢。
 */
function buildSortedOrder(report: ProbeReport, existing: string[]): string[] {
  const alive = report.sources.filter((s) => s.status !== "dead");
  const maxSpeed = Math.max(
    ...alive.filter((s) => s.speedMs != null).map((s) => s.speedMs as number),
    1,
  );

  const ranked = alive
    .map((s) => ({ name: s.name, score: scoreProbe(s.status, s.speedMs, maxSpeed) }))
    .sort((a, b) => b.score - a.score || a.name.localeCompare(b.name))
    .map((x) => x.name);

  const seen = new Set(ranked);
  const deadSet = new Set(report.sources.filter((s) => s.status === "dead").map((s) => s.name));

  // 未参与体检的源：保持原有相对顺序
  const untouched = existing.filter((n) => !seen.has(n) && !deadSet.has(n));
  // 失活的源：也按原顺序排在最末
  const dead = existing.filter((n) => deadSet.has(n));

  return [...ranked, ...untouched, ...dead];
}

/**
 * 版本号是否可解析（用于区分「确实是最新」与「无法比较」）
 */
function isComparableVersion(v: string): boolean {
  return /^\d+(\.\d+){0,2}/.test(String(v || "").trim().replace(/^v/i, ""));
}

const SOURCE_LABELS: Record<string, string> = {
  kw: "酷我",
  wy: "网易云",
  tx: "QQ音乐",
  kg: "酷狗",
  mg: "咪咕",
};

function sourceLabel(src: string) {
  return SOURCE_LABELS[src] || src || "未知";
}

/** 把引擎返回的 interval（秒数或 mm:ss）统一成 mm:ss 展示 */
function formatInterval(v: number | string | null | undefined): string {
  if (v == null || v === "") return "—";
  if (typeof v === "number" || /^\d+(\.\d+)?$/.test(String(v))) {
    const total = Math.round(Number(v));
    if (!total) return "—";
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${m}:${String(s).padStart(2, "0")}`;
  }
  return String(v);
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
  // 已发出停止信号、但引擎还在收尾；此期间禁止再起新任务
  const [stopping, setStopping] = useState(false);
  const [progress, setProgress] = useState<Progress | null>(null);
  const logEndRef = useRef<HTMLDivElement>(null);

  // ===== 单曲搜索 =====
  const [keyword, setKeyword] = useState("");
  const [searching, setSearching] = useState(false);
  const [results, setResults] = useState<Song[]>([]);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [searchHint, setSearchHint] = useState("");

  // ===== 歌单歌曲展开 =====
  const [songs, setSongs] = useState<Song[]>([]);
  const [songsOf, setSongsOf] = useState<string>("");
  /** 歌单列表请求序号：用于丢弃过期响应，避免快速切换时内容错位 */
  const songsRequestRef = useRef(0);
  const [loadingSongs, setLoadingSongs] = useState(false);

  // ===== 音源管理 =====
  const [sourceFiles, setSourceFiles] = useState<string[]>([]);
  const [sourcesDir, setSourcesDir] = useState("");
  const [lxApiPath, setLxApiPath] = useState<string | null>(null);
  const [importingLx, setImportingLx] = useState(false);
  const sourceInputRef = useRef<HTMLInputElement>(null);

  // ===== 音源体检（失活检测 + 测速）=====
  const [probing, setProbing] = useState(false);
  const [probeReport, setProbeReport] = useState<ProbeReport | null>(null);
  /** 体检后用户勾选要删除的失活音源 */
  const [deadPicked, setDeadPicked] = useState<Set<string>>(new Set());
  /** 体检日志面板是否展开 */
  /**
   * 体检过程中的实时日志行。
   *
   * 为什么需要单独一份：体检面板原本以 probeReport 为渲染条件，
   * 而 probeReport 要等全部跑完才有值，导致「运行中看不到任何东西」。
   * 这里把引擎写 stderr 的 [体检] 行实时收集起来，体检期间就能滚动显示。
   */
  const [probeLiveLines, setProbeLiveLines] = useState<string[]>([]);
  /** 体检进行中（与 probing 的区别：probing 表示整个流程，这里表示日志流是否活跃） */
  const probeLogRef = useRef<HTMLDivElement | null>(null);

  // 体检日志实时滚动到底部：新行进来就跟着走，不用手动拖
  useEffect(() => {
    const box = probeLogRef.current;
    if (!box) return;
    box.scrollTop = box.scrollHeight;
  }, [probeLiveLines]);

  // ===== 导出 =====
  const [exportLines, setExportLines] = useState<string[]>([]);
  const [exporting, setExporting] = useState(false);
  const exportText = exportLines.join("\n");

  // ===== 重试上次失败的歌 =====
  // failedCount > 0 才显示按钮。数量由后端 count_failed 读 failed.txt 得出，
  // 前端不自己解析文件（解析逻辑只在引擎里维护一份，避免两处格式定义漂移）。
  const [failedCount, setFailedCount] = useState(0);

  /** 应用版本号（启动时从 Tauri 读，唯一来源 tauri.conf.json） */
  const [appVersion, setAppVersion] = useState("…");

  // ===== 检查更新 =====
  const [checkingUpdate, setCheckingUpdate] = useState(false);
  const [updateInfo, setUpdateInfo] = useState<UpdateInfo | null>(null);
  /** 用户点了「稍后再说」后不再提示（本次会话内） */
  const [updateDismissed, setUpdateDismissed] = useState(false);
  const [downloadingUpdate, setDownloadingUpdate] = useState(false);

  // 日志上限：批量下载 700+ 首会产出数千行，全量渲染 + 每行平滑滚动会明显掉帧。
  // 只保留最近 MAX_LOGS 行（超出后丢弃最旧的），既不影响排障也保证流畅。
  const MAX_LOGS = 800;
  const pushLog = useCallback((line: LogLine) => {
    setLogs((prev) => {
      const next = prev.length >= MAX_LOGS ? prev.slice(prev.length - MAX_LOGS + 1) : prev.slice();
      next.push(line);
      return next;
    });
  }, []);

  const addLog = useCallback(
    (kind: LogLine["kind"], data: string) => {
      pushLog({ kind, data });
      if (kind === "done") setRunning(false);
    },
    [pushLog],
  );

  const handleLog = useCallback(
    (line: LogLine) => {
      pushLog(line);
      // 体检期间把 [体检] 开头的行同步收集到面板里，实现实时滚动。
      // 引擎把这些进度写 stderr，Rust 的 spawn_task 会按行推成 download-log。
      if (line.data.includes("[体检]")) {
        setProbeLiveLines((prev) => {
          const next = prev.concat(line.data.trim());
          // 上限保护：一次体检最多几十行，留 200 足够
          return next.length > 200 ? next.slice(next.length - 200) : next;
        });
      }
      if (line.kind === "done") {
        setRunning(false);
        setStopping(false);
      }
    },
    [pushLog],
  );

  const loadPlaylists = useCallback(async () => {
    setLoadingPl(true);
    try {
      const pls = await invoke<Playlist[]>("list_playlists", {
        db: dbPath || null,
        python: pythonPath || null,
      });
      setPlaylists(pls);
      setSelected((cur) => (cur && pls.some((p) => p.id === cur) ? cur : pls[0]?.id ?? ""));
      addLog("info", `成功读取 ${pls.length} 个歌单`);
    } catch (err) {
      setPlaylists([]);
      // 带上上下文：空字符串错误（Tauri 侧 reject 无 payload 时会出现）
      // 以前会打出一行只有 "error" 两个字、完全没有信息的日志，用户
      // 和我们都没法判断是配置错误、引擎缺失还是数据库被占用。
      addLog("error", `读取歌单失败: ${err instanceof Error ? err.message : String(err) || "未知原因"}`);
    } finally {
      setLoadingPl(false);
    }
  }, [dbPath, pythonPath, addLog]);

  const refreshSources = useCallback(async () => {
    try {
      // get_source_order 已经包含「实际存在 + 用户排序 + 新文件追加」，直接用它
      const [ordered, dirPath, lxPath] = await Promise.all([
        invoke<string[]>("get_source_order"),
        invoke<string>("get_sources_dir"),
        invoke<string | null>("find_lx_desktop_api"),
      ]);
      setSourceFiles(ordered);
      setSourcesDir(dirPath);
      setLxApiPath(lxPath);
    } catch (err) {
      // 同样带上上下文，避免出现只有 "error" 一行、无法定位的日志
      addLog("error", `读取音源列表失败: ${err instanceof Error ? err.message : String(err) || "未知原因"}`);
    }
  }, [addLog]);

  /**
   * 查询下载目录里有多少首失败歌曲（决定是否显示「重试」按钮）。
   *
   * 目录留空时传 null：后端不传值，由引擎退回它自己的默认下载目录
   * （与「开始下载」留空时的落点一致）——这是主路径，大多数用户不填目录。
   * 任何错误都按 0 处理——「没有失败清单」是正常状态，不该在日志区刷错误。
   */
  const refreshFailedCount = useCallback(async () => {
    try {
      const n = await invoke<number>("count_failed", {
        dir: dir.trim() || null,
        python: pythonPath || null,
      });
      setFailedCount(n);
    } catch {
      setFailedCount(0);
    }
  }, [dir, pythonPath]);

  // 三种时机会重新统计：目录变更、python 路径变更、任务结束（running 变 false）。
  // 合成一个 effect 而不是两个，避免同一次变更触发两遍查询。
  // 防抖 400ms：用户手打路径时不该每敲一个字符都起一次引擎子进程。
  useEffect(() => {
    const timer = window.setTimeout(() => {
      refreshFailedCount();
    }, 400);
    return () => window.clearTimeout(timer);
  }, [refreshFailedCount, running]);

  // 首页自动加载（只在挂载时跑一次）。
  // 注意依赖**故意为空**：版本号、更新检查、体检缓存都只该在启动时做一次。
  // 歌曲列表的加载由下面的 effect 单独负责（它才需要跟随 dbPath/pythonPath 变化）。
  useEffect(() => {
    // 载入上次的体检缓存：不必每次进页面都等一两分钟
    invoke<ProbeReport | null>("load_probe_cache")
      .then((r) => { if (r) setProbeReport(r); })
      .catch(() => { /* 没有缓存是正常状态 */ });
    // 版本号从 Tauri 读（唯一来源 tauri.conf.json），避免手写漏改
    getVersion()
      .then((v) => {
        setAppVersion(v);
        // 启动后静默检查更新：拿到版本号再查，才能正确比较。
        // silent=true 表示「没新版/查失败都不打扰用户」。
        // 直接传 v，不依赖 state（setState 是异步的，此刻读 appVersion 还是旧值）
        void checkForUpdate(true, v);
      })
      .catch(() => setAppVersion("unknown"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 歌单：跟随数据库路径 / Python 路径变化重新加载，但加 600ms 防抖。
  // 不加防抖的话，用户在输入框每敲一个字符都会触发一次 list_playlists
  // —— 那是一个最长 60 秒的引擎子进程，输入一个路径等于起几十个进程。
  useEffect(() => {
    const timer = window.setTimeout(() => { loadPlaylists(); }, 600);
    return () => window.clearTimeout(timer);
  }, [loadPlaylists]);

  // 音源列表只需在挂载时加载一次（导入/删除后会显式 refreshSources）
  useEffect(() => {
    refreshSources();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 启动时读取持久化设置。
  // 设置存在引擎的 %APPDATA%\lx-downloader-config.json（与命令行界面共用），
  // 这样「音质 / 最低音质 / 并发 / 下载目录」改完重启就不会丢。
  // 读失败不阻断界面：只是回到各 state 的默认值。
  useEffect(() => {
    let cancelled = false;
    void invoke<Record<string, unknown>>("read_settings", { python: pythonPath || null })
      .then((cfg) => {
        if (cancelled || !cfg) return;
        if (typeof cfg.quality === "string") setQuality(cfg.quality);
        if (typeof cfg.fallback_quality === "boolean") setFallback(cfg.fallback_quality);
        if (typeof cfg.min_quality === "string") setMinQuality(cfg.min_quality);
        if (typeof cfg.workers === "number") setWorkers(cfg.workers);
        // 目录与数据库路径为空时保持界面默认（空串表示「自动探测」）
        if (typeof cfg.dir === "string") setDir(cfg.dir);
        if (typeof cfg.db === "string") setDbPath(cfg.db);
      })
      .catch((err) => {
        // 静默回退到默认值即可，但要留一条可见记录：
        // 设置读失败通常意味着引擎释放出了问题，完全不提示会让用户
        // 下次发现「改了设置没保存」却毫无线索。
        addLog("error", `读取设置失败（将使用默认值）: ${err instanceof Error ? err.message : String(err) || "未知原因"}`);
      });
    return () => { cancelled = true; };
    // 只在挂载时读一次；pythonPath 变化不该重新覆盖用户当前选择
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 设置变化时持久化（防抖 600ms）。
  // 这些控件都是用户在界面里直接改的，改动即保存比「点保存按钮」更顺手；
  // 防抖是为了避免拖动数值输入框时每敲一下就起一个引擎子进程。
  // 首次挂载时也会触发一次，但那次写入的就是刚读出来的值，无副作用。
  const settingsReady = useRef(false);
  useEffect(() => {
    // 跳过挂载后的第一次（此时 read_settings 可能还没回来，
    // 直接写会把用户已保存的设置覆盖成 state 默认值）。
    if (!settingsReady.current) {
      settingsReady.current = true;
      return;
    }
    const timer = window.setTimeout(() => {
      void invoke("save_settings", {
        patch: {
          quality,
          fallback_quality: fallback,
          min_quality: minQuality,
          workers,
          dir,
        },
        python: pythonPath || null,
      }).catch(() => { /* 保存失败不打断使用 */ });
    }, 600);
    return () => window.clearTimeout(timer);
  }, [quality, fallback, minQuality, workers, dir, pythonPath]);

  // 自动滚到底部：节流到 200ms 一次，且只在用户没有手动上翻时滚动。
  // 每条日志都 smooth scroll 会在批量下载时明显掉帧。
  useEffect(() => {
    const box = logEndRef.current?.parentElement;
    if (!box) return;
    // 用户往上翻看历史时不打扰
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    if (!atBottom) return;
    const timer = window.setTimeout(() => {
      logEndRef.current?.scrollIntoView({ behavior: "auto", block: "end" });
    }, 200);
    return () => window.clearTimeout(timer);
  }, [logs]);

  // 订阅下载日志
  useEffect(() => {
    // cancelled 标志：listen() 是异步的，若在它 resolve 之前组件就卸载，
    // 原写法 unH 还没被赋值，cleanup 里的 unH?.() 是空操作，
    // 监听器会永久残留（StrictMode 双挂载时会重复订阅，日志每条触发两次）。
    let cancelled = false;
    let unH: (() => void) | undefined;
    (async () => {
      const off = await listen<LogLine>("download-log", (e) => handleLog(e.payload));
      if (cancelled) off();        // 已卸载：立刻注销
      else unH = off;
    })();
    return () => {
      cancelled = true;
      unH?.();
    };
  }, [handleLog]);

  // 订阅进度
  useEffect(() => {
    let cancelled = false;
    let unH: (() => void) | undefined;
    (async () => {
      const off = await listen<string>("progress", (e) => {
        try {
          const p = JSON.parse(e.payload) as Progress;
          setProgress(p);
          // 注意：进度到 100% 不代表进程已退出（还有二次重试、写失败清单）。
          // running 只由 done 事件解除，否则停止后引擎仍在收尾时按钮会提前可点。
        } catch {
          /* ignore badly-formed progress */
        }
      });
      if (cancelled) off();
      else unH = off;
    })();
    return () => {
      cancelled = true;
      unH?.();
    };
  }, []);

  function clearLogs() {
    setLogs([]);
  }

  // ===================== 下载动作 =====================

  function downloadArgs() {
    return {
      quality,
      dir: dir || null,
      workers,
      fallback,
      minQuality,
      python: pythonPath || null,
    };
  }

  async function startPlaylistDownload() {
    if (!selected) {
      addLog("error", "请先选择一个歌单");
      return;
    }
    setRunning(true);
    setProgress({ done: 0, total: 0, success: 0, fail: 0, skip: 0 });
    const pl = playlists.find((p) => p.id === selected);
    addLog("info", `开始下载歌单「${pl?.name ?? selected}」…（音质 ${quality}）`);
    try {
      await invoke("start_download", {
        playlist: selected,
        db: dbPath || null,
        ...downloadArgs(),
      });
    } catch (err) {
      addLog("error", String(err));
      setRunning(false);
    }
  }

  /** 下载一组歌曲（搜索结果 / 歌单内单曲都走这里） */
  async function downloadSongs(list: Song[], label: string) {
    if (list.length === 0) return;
    setRunning(true);
    setProgress({ done: 0, total: 0, success: 0, fail: 0, skip: 0 });
    addLog("info", `开始下载 ${list.length} 首（${label}），音质 ${quality}`);
    try {
      await invoke("download_songs", {
        songs: list.map((s) => ({
          name: s.name,
          singer: s.singer,
          source: s.source,
          interval: s.interval ?? null,
          id: s.id,
          songmid: s.songmid ?? s.id,
          // meta 里带的是数据库原始纯数字 ID（如 songId=30070747），
          // 少了它，音源就认不出带平台前缀的 id（wy_30070747）。
          meta: s.meta ?? null,
        })),
        ...downloadArgs(),
      });
    } catch (err) {
      addLog("error", String(err));
      setRunning(false);
    }
  }

  /** 只重试上次失败的歌：读 <下载目录>/failed.txt，只下载清单里的歌 */
  async function retryFailed() {
    setRunning(true);
    setProgress({ done: 0, total: 0, success: 0, fail: 0, skip: 0 });
    const where = dir.trim() || "默认下载目录";
    addLog("info", `开始重试上次失败的 ${failedCount} 首歌（${where}，音质 ${quality}）`);
    try {
      // 目录留空时传 null，由引擎用默认目录（与「开始下载」留空时完全一致）。
      // 不能用 downloadArgs() 展开后再覆盖 dir —— 同名字段会触发 TS2783。
      const args = downloadArgs();
      await invoke("retry_failed", {
        ...args,
        dir: dir.trim() || null,
      });
    } catch (err) {
      addLog("error", String(err));
      setRunning(false);
    }
  }

  async function stop() {
    try {
      addLog("info", "已请求停止，正在等待在途任务收尾…");
      // 后端只做「写信号 + 立即返回」，不会阻塞界面；真正的等待在后台线程。
      const mode = await invoke<string>("stop_task");
      if (mode === "stopping") {
        // 保持 running=true：引擎退出时会推 done 事件，那时才真正解除。
        // 若这里立刻置 false，用户可能在收尾期间就点「开始下载」，
        // 新旧任务会争抢同一个引擎目录与停止信号文件。
        addLog("info", "停止信号已发出：引擎会先收尾（保留已下载文件、清理半成品），超时才强制结束");
        setStopping(true);
        return;
      }
      if (mode === "starting") {
        // 引擎还在解压/启动，没有子进程可停。停止信号已写入，
        // 引擎启动后会立刻看到并退出；这里保持 running，等 done 事件。
        addLog("info", "引擎正在启动，已请求其在启动完成后立即退出");
        setStopping(true);
        return;
      }
      if (mode === "killing") {
        addLog("error", "停止信号写入失败，正在强制结束进程");
        setStopping(true);
        return;
      }
      addLog("info", "当前没有正在运行的任务");
      setRunning(false);
      setStopping(false);
    } catch (err) {
      addLog("error", String(err));
      // 停止请求本身失败（IPC 异常等）时不能什么都不做：
      // running 仍为 true、stopping 为 false，而 28s 兜底定时器依赖 stopping，
      // 界面会永久停在「任务运行中」且所有按钮禁用。
      // 退一步进入「正在停止」，让兜底定时器接管解锁。
      setStopping(true);
    }
  }

  // ===== 音源拖拽排序 =====
  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const [dropIndex, setDropIndex] = useState<number | null>(null);
  const [reordering, setReordering] = useState(false);

  /** 把 from 位置的音源移动到 to 位置，并立刻持久化 */
  const moveSource = useCallback(
    async (to: number) => {
      const from = dragIndex;
      setDragIndex(null);
      setDropIndex(null);
      if (from == null || from === to) return;

      const next = [...sourceFiles];
      const [moved] = next.splice(from, 1);
      next.splice(to, 0, moved);
      setSourceFiles(next); // 先乐观更新，拖拽手感不卡

      setReordering(true);
      try {
        await invoke("set_source_order", { order: next });
        addLog("ok", `音源优先级已更新：${moved} → 第 ${to + 1} 位`);
      } catch (err) {
        addLog("error", `保存音源顺序失败: ${err}`);
        await refreshSources(); // 失败则回滚到磁盘上的真实顺序
      } finally {
        setReordering(false);
      }
    },
    [dragIndex, sourceFiles, addLog, refreshSources],
  );

  // ===================== 搜索 =====================

  async function doSearch() {
    const kw = keyword.trim();
    if (!kw) {
      addLog("error", "请输入歌名或「歌手 歌名」");
      return;
    }
    setSearching(true);
    setResults([]);
    setPicked(new Set());
    setSearchHint("");
    addLog("info", `搜索「${kw}」…`);
    try {
      const list = await invoke<Song[]>("search_songs", {
        keyword: kw,
        limit: 30,
        python: pythonPath || null,
      });
      setResults(list);
      if (list.length === 0) {
        setSearchHint("没有找到结果，换个关键词试试（可用「歌手 歌名」）");
        addLog("info", "搜索完成，没有匹配结果");
      } else {
        addLog("ok", `搜索完成，共 ${list.length} 个结果`);
      }
    } catch (err) {
      setSearchHint(String(err));
      addLog("error", String(err));
    } finally {
      setSearching(false);
    }
  }

  function togglePick(key: string) {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  const songKey = (s: Song) => `${s.source}|${s.id}|${s.name}|${s.singer}`;

  // ===================== 歌单歌曲 =====================

  async function toggleSongs(playlistId: string) {
    if (songsOf === playlistId) {
      setSongsOf("");
      setSongs([]);
      return;
    }
    // 请求序号：快速点 A→B 时，若 A 的响应后到会覆盖 B 的歌曲列表，
    // 造成「标题是 B、内容却是 A 的歌」，点「下载整张歌单」就会下错歌。
    // 每次请求递增，await 回来后只接受最新一次的结果。
    const token = ++songsRequestRef.current;
    setLoadingSongs(true);
    setSongs([]);
    setSongsOf(playlistId);
    try {
      const list = await invoke<Song[]>("list_songs", {
        playlist: playlistId,
        db: dbPath || null,
        python: pythonPath || null,
      });
      // 已有更新的请求发出 → 丢弃这次结果（连日志一起丢弃，避免误导）
      if (token !== songsRequestRef.current) return;
      setSongs(list);
      const pl = playlists.find((p) => p.id === playlistId);
      addLog("info", `歌单\u300c${pl?.name ?? playlistId}\u300d共 ${list.length} 首`);
    } catch (err) {
      if (token !== songsRequestRef.current) return;
      addLog("error", String(err));
      setSongsOf("");
    } finally {
      if (token === songsRequestRef.current) setLoadingSongs(false);
    }
  }

  // ===================== 音源 =====================

  async function onPickSource(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    try {
      const text = await new Promise<string>((resolve, reject) => {
        const r = new FileReader();
        r.onload = () => resolve(String(r.result));
        r.onerror = () => reject(new Error("读取音源文件失败"));
        r.readAsText(file);
      });
      const name = await invoke<string>("import_source", {
        fileName: file.name,
        content: text,
      });
      addLog("ok", `已导入音源: ${name}`);
      await refreshSources();
    } catch (err) {
      addLog("error", String(err));
    }
  }

  async function removeSource(name: string) {
    try {
      await invoke("remove_source", { fileName: name });
      addLog("info", `已删除音源: ${name}`);
      await refreshSources();
    } catch (err) {
      addLog("error", String(err));
    }
  }

  /** 检查更新：有新版就提示用户，由用户决定是否下载 */
  async function checkForUpdate(silent = false, currentVersion?: string) {
    if (checkingUpdate) return;
    const cur = currentVersion ?? appVersion;
    setCheckingUpdate(true);
    try {
      const info = await invoke<UpdateInfo>("check_update", {
        python: pythonPath || null,
      });
      if (!info.ok) {
        if (!silent) addLog("error", `检查更新失败: ${info.error || "未知原因"}`);
        return;
      }
      // 版本比较：只在远端严格大于本地时提示
      // 若当前版本读不到（getVersion 失败 / 值异常），compareVersion 返回 0，
      // 不能把它当成「已是最新」——那会让用户以为已经最新而错过更新。
      if (!isComparableVersion(cur)) {
        if (!silent) {
          addLog("error", `无法获取当前版本（${cur}），已跳过比较；最新版本为 ${info.latest}`);
        }
        return;
      }
      const cmp = compareVersion(info.latest, cur);
      if (cmp > 0) {
        setUpdateInfo(info);
        setUpdateDismissed(false);
        addLog("ok", `发现新版本 ${info.latest}（当前 ${cur}）`);
      } else if (!silent) {
        addLog("info", `已是最新版本（${cur}）`);
      }
    } catch (err) {
      if (!silent) addLog("error", String(err));
    } finally {
      setCheckingUpdate(false);
    }
  }

  /** 下载更新包：让用户选保存位置，然后下载到那里 */
  async function downloadUpdate() {
    if (!updateInfo?.downloadUrl) {
      addLog("error", "该版本没有提供安装包，请到发布页手动下载");
      return;
    }
    let path: string | null = null;
    try {
      path = await save({
        title: "保存更新包",
        defaultPath: updateInfo.assetName || `落雪音乐下载器-${updateInfo.latest}.exe`,
        filters: [{ name: "可执行文件", extensions: ["exe"] }],
      });
    } catch (err) {
      // 对话框本身失败（权限/后端异常）时给出明确反馈，
      // 否则会变成未处理的 Promise rejection，用户点了没反应。
      addLog("error", `打开保存对话框失败: ${err}`);
      return;
    }
    if (!path) return;   // 用户取消

    setDownloadingUpdate(true);
    addLog("info", `正在下载 ${updateInfo.latest}…`);
    try {
      await invoke<string>("download_update", {
        url: updateInfo.downloadUrl,
        path,
      });
      addLog("ok", `下载完成：${path}`);
      addLog("info", "请关闭本程序后，用新下载的文件替换旧程序即可完成升级");
    } catch (err) {
      addLog("error", String(err));
    } finally {
      setDownloadingUpdate(false);
    }
  }

  /**
   * 等待体检子进程真正结束。
   *
   * 为什么不用 done 事件：done 由 stdout 读到 EOF 时触发，
   * 而体检的结果是在进程退出前最后才写的——两者时序接近但不可靠；
   * 更稳妥的是轮询 is_task_running（纯内存读，开销极小）。
   * 同时设 10 分钟上限，避免引擎异常时永久挂住界面。
   */
  async function waitForProbeDone() {
    const deadline = Date.now() + 10 * 60 * 1000;
    // 先等 300ms，避开「刚 spawn 还没登记 child」的窗口
    await new Promise((r) => setTimeout(r, 300));
    while (Date.now() < deadline) {
      try {
        const busy = await invoke<boolean>("is_task_running");
        if (!busy) return;
      } catch {
        /* 查询失败按「还在跑」处理，下一轮再试 */
      }
      await new Promise((r) => setTimeout(r, 400));
    }
  }

  async function probeSources() {
    if (probing) return;
    // 双重保险（按钮已 disabled，这里再拦一次）：体检会杀掉后端正在跑的任务，
    // 所以下载中/停止中绝不允许开始体检。
    if (running || stopping) {
      addLog("info", "请等当前下载任务结束后再体检");
      return;
    }
    setProbing(true);
    // 清空上一轮实时日志（面板会自动出现在音源列表下方）
    setProbeLiveLines([]);
    // 清掉上一轮结果：否则重新体检期间会一直显示旧结果，看不出在跑
    setProbeReport(null);
    // 同时清掉上一轮的勾选。否则体检失败/超时后 probeReport 为 null，
    // 但 deadPicked 还留着上次的音源名 —— 界面会出现「已勾选 N 个失活音源」
    // 却看不到任何复选框（复选框依赖 probeReport），点删除会删掉看不见的文件。
    setDeadPicked(new Set());
    addLog("info", "开始音源体检（逐个实测取链接 + 测速），可能需要一两分钟…");
    try {
      // 流式跑：日志由 download-log 事件实时推过来（引擎把进度写 stderr），
      // probe_sources 只负责「启动」，会立刻返回 "started"；
      // 必须等 done 事件（进程真正结束）再去读缓存，否则读到的是空文件。
      await invoke<string>("probe_sources", { python: pythonPath || null });
      await waitForProbeDone();
      const report = await invoke<ProbeReport | null>("load_probe_cache");
      if (!report) {
        addLog("error", "体检未产出结果，请查看上方日志");
        return;
      }
      setProbeReport(report);

      const dead = report.sources.filter((s) => s.status === "dead");
      const flaky = report.sources.filter((s) => s.status === "flaky");
      const ok = report.sources.filter((s) => s.status === "ok");

      addLog(
        dead.length ? "error" : "ok",
        `体检完成：正常 ${ok.length} 个，不稳定 ${flaky.length} 个，失活 ${dead.length} 个`,
      );
      // 默认勾选失活音源，用户可自行取消
      setDeadPicked(new Set(dead.map((s) => s.name)));
    } catch (err) {
      addLog("error", String(err));
    } finally {
      setProbing(false);
    }
  }
  /**
   * 按体检结果重排音源优先级（稳定性 → 速度），并写入 .order.json。
   *
   * 这会改变实际下载时的音源尝试顺序——不只是界面上好看：
   * 排在前面的源会被优先调用，稳定又快的源先上，能减少失败重试。
   */
  async function applyProbeOrder() {
    if (!probeReport || reordering) return;
    const next = buildSortedOrder(probeReport, sourceFiles);
    if (!next.length) {
      addLog("error", "没有可用于排序的音源");
      return;
    }
    setReordering(true);
    try {
      await invoke("set_source_order", { order: next });
      await refreshSources();
      const alive = probeReport.sources.filter((s) => s.status !== "dead").length;
      addLog("ok", `已按体检结果排序：${alive} 个可用音源按「稳定 → 快」重排，优先尝试靠前的源`);
    } catch (err) {
      addLog("error", `保存排序失败: ${err}`);
      await refreshSources();
    } finally {
      setReordering(false);
    }
  }



  /** 删除体检中勾选的失活音源（用户确认后才会走到这里） */
  async function deletePickedSources() {
    const names = Array.from(deadPicked);
    if (!names.length) return;
    try {
      const removed = await invoke<string[]>("remove_sources", { names });
      addLog("ok", `已删除 ${removed.length} 个音源${removed.length ? "：" + removed.join("、") : ""}`);
      setDeadPicked(new Set());
      setProbeReport((prev) =>
        prev ? { ...prev, sources: prev.sources.filter((s) => !removed.includes(s.name)) } : prev,
      );
      await refreshSources();
    } catch (err) {
      addLog("error", String(err));
    }
  }

  /** 从落雪音乐桌面端导入音源（自动探测 user_api.json，也可手动选文件） */
  async function importFromLxDesktop(manualPath?: string) {
    setImportingLx(true);
    try {
      const names = await invoke<string[]>("import_lx_desktop_sources", {
        path: manualPath ?? null,
      });
      addLog("ok", `已从落雪音乐导入 ${names.length} 个音源：${names.join("、")}`);
      await refreshSources();
    } catch (err) {
      addLog("error", String(err));
    } finally {
      setImportingLx(false);
    }
  }

  async function pickLxApiFile() {
    const picked = await openDialog({
      title: "选择落雪音乐的 user_api.json",
      multiple: false,
      filters: [{ name: "落雪音源", extensions: ["json"] }],
    });
    if (typeof picked === "string") {
      await importFromLxDesktop(picked);
    }
  }

  // ===================== 导出 =====================

  async function doExport() {
    if (!selected) {
      addLog("error", "请先选择一个歌单");
      return;
    }
    setExporting(true);
    setExportLines([]);
    try {
      const lines = await invoke<string[]>("export_songs", {
        playlist: selected,
        db: dbPath || null,
        python: pythonPath || null,
      });
      setExportLines(lines);
      const pl = playlists.find((p) => p.id === selected);
      addLog("ok", `已导出歌单「${pl?.name ?? selected}」，共 ${lines.length} 首`);
    } catch (err) {
      addLog("error", String(err));
    } finally {
      setExporting(false);
    }
  }

  async function copyExport() {
    if (!exportText) return;
    try {
      await navigator.clipboard.writeText(exportText);
      addLog("ok", "清单已复制到剪贴板");
      return;
    } catch {
      try {
        const ta = document.createElement("textarea");
        ta.value = exportText;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
        addLog("ok", "清单已复制到剪贴板");
      } catch {
        addLog("error", "复制失败，请手动全选复制");
      }
    }
  }

  async function saveExport() {
    if (!exportText) return;
    const pl = playlists.find((p) => p.id === selected);
let path: string | null = null;
    try {
        path = await save({
        title: "另存歌单歌曲清单",
        defaultPath: `${pl?.name || "歌单"}.txt`,
        filters: [{ name: "文本文件", extensions: ["txt"] }],
      });
    } catch (err) {
      addLog("error", `打开保存对话框失败: ${err}`);
      return;
    }
    if (!path) return;
    try {
      await invoke("save_text_file", { path, content: exportText + "\n" });
      addLog("ok", `已保存: ${path}`);
    } catch (err) {
      addLog("error", String(err));
    }
  }

  async function copyText(text: string, okMsg: string) {
    try {
      await navigator.clipboard.writeText(text);
      addLog("ok", okMsg);
    } catch {
      addLog("error", "复制失败，请手动选择复制");
    }
  }

  async function openExternal(url: string) {
    try {
      await openUrl(url);
    } catch (err) {
      addLog("error", `打开链接失败: ${err}`);
    }
  }

  // 停止的兜底：后端最多等 20s 后强杀，这里给 28s。
  // 万一 done 事件因为进程被强杀而没送达，界面也不会永远卡在「正在停止」。
  useEffect(() => {
    if (!stopping) return;
    const timer = window.setTimeout(() => {
      setStopping(false);
      setRunning(false);
      addLog("error", "停止超时：已解除界面锁定，若仍有残留进程请重新开启应用");
    }, 28000);

    // 早期校正：若引擎其实早已退出（done 事件在 stop 之前就已错过），
    // 1.5s 后主动探测一次任务状态，避免无意义地等满 28 秒。
    const probe = window.setTimeout(async () => {
      try {
        const busy = await invoke<boolean>("is_task_running");
        if (!busy) {
          setStopping(false);
          setRunning(false);
        }
      } catch {
        /* 探测失败就交给 28s 兜底 */
      }
    }, 1500);

    return () => {
      window.clearTimeout(timer);
      window.clearTimeout(probe);
    };
  }, [stopping, addLog]);

  // 统计成功行数。原来直接 `logs.filter(...)` 写在渲染体里，
  // 而下载中 progress 事件频繁触发重渲染，等于每次绘制都全量扫一遍
  // （最多 800 条）——用 useMemo 只在 logs 变化时才重算。
  const okCount = useMemo(() => logs.filter((l) => l.kind === "ok").length, [logs]);
  const pickedSongs = results.filter((s) => picked.has(songKey(s)));

  const NAV: [Tab, string, React.ReactNode][] = [
    ["playlists", "我的歌单", <ListIcon size={17} />],
    ["search", "搜索单曲", <SearchIcon size={17} />],
    ["download", "下载歌曲", <DownloadIcon size={17} />],
    ["export", "导出歌单", <ExportIcon size={17} />],
    ["sources", "音源", <PlugIcon size={17} />],
    ["settings", "设置", <SettingsIcon size={17} />],
    ["about", "关于", <InfoIcon size={17} />],
  ];

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-icon">
            <BrandMark size={44} />
          </div>
          <div className="brand-text">
            <h1>落雪下载</h1>
            <span>Lossless · 320k</span>
          </div>
        </div>
        <nav>
          {NAV.map(([key, label, icon]) => (
            <button
              key={key}
              className={`nav-item ${tab === key ? "active" : ""}`}
              onClick={() => setTab(key)}
            >
              <span className="nav-icon">{icon}</span>
              {label}
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <span className={`status-dot ${running ? "pulse" : ""}`} />
          {stopping ? "正在停止…" : running ? "任务运行中" : "空闲"}
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
              <button className="danger-btn" onClick={stop} disabled={stopping}>
                {stopping ? "正在停止…" : "停止"}
              </button>
            )}
          </div>
        </header>

        {/* ===== 新版本提示条 ===== */}
        {updateInfo && !updateDismissed && (
          <div className="update-banner">
            <div className="update-banner-text">
              <strong>发现新版本 {updateInfo.latest}</strong>
              <span>
                当前版本 {appVersion}
                {updateInfo.name ? ` ｜ ${updateInfo.name}` : ""}
              </span>
            </div>
            <div className="update-banner-actions">
              <button
                className="primary-btn small"
                onClick={downloadUpdate}
                disabled={downloadingUpdate || !updateInfo.downloadUrl}
                title={updateInfo.downloadUrl ? "" : "该版本未提供安装包，请到发布页下载"}
              >
                {downloadingUpdate ? "下载中…" : "下载更新"}
              </button>
              <button
                className="ghost-btn small"
                onClick={() => openExternal(updateInfo.pageUrl || AUTHOR.repo + "/releases")}
              >
                查看详情
              </button>
              <button className="ghost-btn small" onClick={() => setUpdateDismissed(true)}>
                稍后
              </button>
            </div>
          </div>
        )}

        {progress && (
          <div className="progressbar-wrap">
            <div className="progress-meta">
              <span>
                {progress.total > 0
                  ? `进度 ${progress.done}/${progress.total}`
                  : "准备中…"}
              </span>
              <span className="progress-stats">
                <em className="st-ok">成功 {progress.success}</em>
                <em className="st-fail">失败 {progress.fail}</em>
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
          {/* ==================== 我的歌单 ==================== */}
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
                歌单文件路径读取。点歌单卡片可展开歌曲列表，逐首下载。
              </p>

              {playlists.length > 0 && (
                <div className="playlist-grid">
                  {playlists.map((p) => (
                    <button
                      key={p.id}
                      className={`playlist-card ${selected === p.id ? "selected" : ""}`}
                      onClick={() => {
                        setSelected(p.id);
                        toggleSongs(p.id);
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
                  <div className="empty-icon">
                    <DiscIcon size={44} />
                  </div>
                  <p>暂时没有读取到歌单</p>
                  <p className="hint">请确认落雪音乐桌面版已运行，或手动填入数据库 / 歌单路径</p>
                </div>
              )}

              {songsOf && (
                <div className="song-panel">
                  <div className="song-panel-head">
                    <strong>
                      {playlists.find((p) => p.id === songsOf)?.name ?? songsOf}
                    </strong>
                    <span className="hint">共 {songs.length} 首</span>
                    <button
                      className="primary-btn small"
                      disabled={running || stopping || probing || songs.length === 0}
                      onClick={() =>
                        downloadSongs(
                          songs,
                          `歌单「${playlists.find((p) => p.id === songsOf)?.name ?? songsOf}」`,
                        )
                      }
                    >
                      下载整张歌单
                    </button>
                    <button className="ghost-btn" onClick={() => setSongsOf("")}>
                      收起
                    </button>
                  </div>
                  {loadingSongs ? (
                    <p className="hint">正在读取歌曲…</p>
                  ) : (
                    <div className="song-table">
                      {songs.map((s, i) => (
                        <div className="song-row" key={`${s.id}-${i}`}>
                          <span className="song-idx">{i + 1}</span>
                          <span className="song-name" title={s.name}>
                            {s.name}
                          </span>
                          <span className="song-singer" title={s.singer}>
                            {s.singer || "—"}
                          </span>
                          <span className="song-src">{sourceLabel(s.source)}</span>
                          <span className="song-time">{formatInterval(s.interval)}</span>
                          <button
                            className="icon-btn"
                            title="下载这首"
                            disabled={running || stopping || probing}
                            onClick={() => downloadSongs([s], `单曲「${s.name}」`)}
                          >
                            <DownloadIcon size={16} />
                          </button>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              <LogView logs={logs} logEndRef={logEndRef} />
            </section>
          )}

          {/* ==================== 搜索单曲 ==================== */}
          {tab === "search" && (
            <section className="panel">
              <div className="panel-row">
                <label className="field grow">
                  <span>搜索歌曲（支持「歌手 歌名」，也可只输歌名）</span>
                  <input
                    value={keyword}
                    onChange={(e) => setKeyword(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !searching) doSearch();
                    }}
                    placeholder="例如：周杰伦 晴天"
                  />
                </label>
                <button className="primary-btn" onClick={doSearch} disabled={searching}>
                  {searching ? "搜索中…" : "搜索"}
                </button>
                {picked.size > 0 && (
                  <button
                    className="primary-btn"
                    disabled={running || stopping || probing}
                    onClick={() => downloadSongs(pickedSongs, "搜索结果选中项")}
                  >
                    下载选中 {picked.size} 首
                  </button>
                )}
              </div>

              <p className="hint">
                搜索结果来自各平台音源；勾选后可批量下载，或直接点单曲右侧的下载按钮。
                下载同样经过时长 / 版本词 / 歌手 / 音质四重校验。
              </p>

              {searchHint && <p className="hint">{searchHint}</p>}

              {results.length > 0 && (
                <div className="song-table">
                  <div className="song-row song-row-head">
                    <span className="song-idx" />
                    <span className="song-name">歌名</span>
                    <span className="song-singer">歌手</span>
                    <span className="song-src">平台</span>
                    <span className="song-time">时长</span>
                    <span className="song-act">操作</span>
                  </div>
                  {results.map((s) => {
                    const k = songKey(s);
                    return (
                      <div className="song-row" key={k}>
                        <span className="song-idx">
                          <input
                            type="checkbox"
                            checked={picked.has(k)}
                            onChange={() => togglePick(k)}
                            aria-label={`选择 ${s.name}`}
                          />
                        </span>
                        <span className="song-name" title={s.name}>
                          {s.name}
                          {s.album ? <em className="song-album"> · {s.album}</em> : null}
                        </span>
                        <span className="song-singer" title={s.singer}>
                          {s.singer || "—"}
                        </span>
                        <span className="song-src" title={s.via}>
                          {sourceLabel(s.source)}
                        </span>
                        <span className="song-time">{formatInterval(s.interval)}</span>
                        <span className="song-act">
                          <button
                            className="icon-btn"
                            title="下载这首"
                            disabled={running || stopping || probing}
                            onClick={() => downloadSongs([s], `单曲「${s.name}」`)}
                          >
                            <DownloadIcon size={16} />
                          </button>
                        </span>
                      </div>
                    );
                  })}
                </div>
              )}

              {!searching && results.length === 0 && !searchHint && (
                <div className="empty">
                  <div className="empty-icon">
                    <SearchIcon size={40} />
                  </div>
                  <p>输入歌名或「歌手 歌名」开始搜索</p>
                </div>
              )}

              <LogView logs={logs} logEndRef={logEndRef} />
            </section>
          )}

          {/* ==================== 下载歌曲（歌单） ==================== */}
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
                    <option value="128k">128k (标准)</option>
                    <option value="192k">192k (较高)</option>
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
                    onChange={(e) => {
                      // 必须钳制成整数：Rust 侧参数是 usize，输入 1.5 或空串会在
                      // 反序列化阶段失败，前端只能看到一条难以理解的 invoke 错误。
                      // 0 表示「自动」，上限 32（与引擎内的 clamp 一致）。
                      const raw = e.target.value.trim();
                      if (raw === "") {
                        setWorkers(0);
                        return;
                      }
                      const n = Math.trunc(Number(raw));
                      if (Number.isNaN(n)) return;
                      setWorkers(Math.max(0, Math.min(32, n)));
                    }}
                  />
                </label>
              </div>

              <div className="panel-row">
                <button
                  className="primary-btn big"
                  onClick={startPlaylistDownload}
                  disabled={running || stopping || probing || playlists.length === 0}
                >
                  {stopping ? "正在停止…" : running ? "正在下载…" : "开始下载"}
                </button>
                {/* 只有下载目录里确实存在失败清单时才出现，避免常态干扰 */}
                {failedCount > 0 && (
                  <button
                    className="ghost-btn"
                    onClick={retryFailed}
                    disabled={running || stopping || probing}
                    title="读取下载目录下的 failed.txt，只补下其中失败过的歌曲"
                  >
                    重试上次失败的 {failedCount} 首
                  </button>
                )}
                {playlists.length === 0 && (
                  <span className="hint">请先在「我的歌单」页加载歌单</span>
                )}
              </div>

              {failedCount > 0 && (
                <p className="hint">
                  「重试」只下载 <code>failed.txt</code> 里记录的歌；已存在的会自动跳过，
                  补下成功的会从清单中移除。
                </p>
              )}

              <LogView logs={logs} logEndRef={logEndRef} />
            </section>
          )}

          {/* ==================== 导出 ==================== */}
          {tab === "export" && (
            <section className="panel">
              <div className="panel-row">
                <label className="field grow">
                  <span>选择歌单</span>
                  <select value={selected} onChange={(e) => setSelected(e.target.value)}>
                    {playlists.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}（{p.cnt} 首）
                      </option>
                    ))}
                  </select>
                </label>
                <button
                  className="primary-btn"
                  onClick={doExport}
                  disabled={exporting || playlists.length === 0}
                >
                  {exporting ? "导出中…" : "导出歌单txt"}
                </button>
                <button className="ghost-btn" onClick={loadPlaylists}>
                  刷新歌单
                </button>
              </div>

              <p className="hint">
                导出为「歌手 - 歌名」清单，按歌单原顺序，同一首歌多平台记录只保留一行。
              </p>

              {exportLines.length > 0 ? (
                <>
                  <div className="export-toolbar">
                    <span className="export-count">
                      共 {exportLines.length} 首
                      {exportLines.length <
                        (playlists.find((p) => p.id === selected)?.cnt ?? 0) &&
                        "（已按歌手+歌名去重）"}
                    </span>
                    <button className="ghost-btn" onClick={copyExport}>
                      复制
                    </button>
                    <button className="ghost-btn" onClick={saveExport}>
                      另存为 .txt
                    </button>
                  </div>
                  <textarea
                    className="export-box"
                    readOnly
                    value={exportText}
                    spellCheck={false}
                  />
                </>
              ) : (
                <div className="empty">
                  <div className="empty-icon">
                    <FileTextIcon size={44} />
                  </div>
                  <p>点击「导出歌单txt」生成歌曲清单</p>
                </div>
              )}

              <LogView logs={logs} logEndRef={logEndRef} />
            </section>
          )}

          {/* ==================== 音源 ==================== */}
          {tab === "sources" && (
            <section className="panel">
              <div className="lx-import-card">
                <div className="lx-import-main">
                  <div className="lx-import-title">
                    <PlugIcon size={18} />
                    从落雪音乐桌面端导入
                  </div>
                  <p className="hint">
                    自动读取落雪音乐 App 里已添加的音源（
                    <code>user_api.json</code>
                    ），逐个解码后存入本程序音源目录，无需手工复制。
                  </p>
                  <code className="sources-dir">
                    {lxApiPath ?? "未检测到落雪音乐的音源文件"}
                  </code>
                </div>
                <div className="lx-import-actions">
                  <button
                    className="primary-btn"
                    onClick={() => importFromLxDesktop()}
                    disabled={importingLx || !lxApiPath}
                    title={lxApiPath ? "从检测到的文件导入" : "未检测到落雪音源文件"}
                  >
                    {importingLx ? "导入中…" : "一键导入"}
                  </button>
                  <button className="ghost-btn" onClick={pickLxApiFile} disabled={importingLx}>
                    手动选择文件
                  </button>
                </div>
              </div>

              <div className="settings-grid">
                <label className="field">
                  <span>音源目录（导入的音源保存在这里，不随程序更新丢失）</span>
                  <code className="sources-dir">{sourcesDir || "…"}</code>
                </label>
              </div>

              <div className="sources-toolbar">
                <button
                  className="primary-btn"
                  onClick={() => sourceInputRef.current?.click()}
                  disabled={importingLx}
                >
                  导入音源文件
                </button>
                <button className="ghost-btn" onClick={refreshSources}>
                  刷新
                </button>
                <button
                  className="ghost-btn"
                  onClick={probeSources}
                  // 关键：下载中/停止中也必须禁用。
                  // 体检和下载共用后端的 TaskState.child（spawn_task 会先杀掉上一个任务），
                  // 允许在下载时体检 = 静默杀掉用户正在跑的下载。
                  disabled={probing || running || stopping || sourceFiles.length === 0}
                  title={
                    running || stopping
                      ? "请等当前下载任务结束后再体检"
                      : "逐个实测每个音源能否真的取到可下载链接，并测出响应速度"
                  }
                >
                  {probing ? "体检中…" : "音源体检"}
                </button>
                <input
                  ref={sourceInputRef}
                  type="file"
                  accept=".js,.json"
                  style={{ display: "none" }}
                  onChange={onPickSource}
                />
              </div>

              {sourceFiles.length > 1 && (
                <p className="hint">
                  按住音源左侧的 <strong>⠿</strong> 拖拽可调整优先级：越靠上越先被尝试。
                  顺序会保存到音源目录，下次启动依然生效。
                </p>
              )}

              <div className="source-list">
                {sourceFiles.length === 0 ? (
                  <p className="hint">
                    尚未导入音源。可点上方「一键导入」从落雪音乐读取，或手动导入 .js / .json
                    音源文件。没有音源时下载会提示「请先导入音源」。
                  </p>
                ) : (
                  sourceFiles.map((name, index) => (
                    <div
                      key={name}
                      className={`source-item ${dragIndex === index ? "dragging" : ""} ${
                        dropIndex === index ? "drop-target" : ""
                      }`}
                      draggable={!reordering}
                      onDragStart={(e) => {
                        setDragIndex(index);
                        e.dataTransfer.effectAllowed = "move";
                        // Firefox 需要设置数据才会触发 drag
                        e.dataTransfer.setData("text/plain", name);
                      }}
                      onDragOver={(e) => {
                        e.preventDefault();
                        e.dataTransfer.dropEffect = "move";
                        if (dropIndex !== index) setDropIndex(index);
                      }}
                      onDragLeave={() => {
                        setDropIndex((d) => (d === index ? null : d));
                      }}
                      onDrop={(e) => {
                        e.preventDefault();
                        moveSource(index);
                      }}
                      onDragEnd={() => {
                        setDragIndex(null);
                        setDropIndex(null);
                      }}
                      title="按住左侧手柄拖拽可调整优先级"
                    >
                      <span className="drag-handle" aria-hidden>
                        ⠿
                      </span>
                      <span className="source-rank">{index + 1}</span>
                      <span className="source-name">{name}</span>
                      <button
                        className="danger-btn"
                        onClick={() => removeSource(name)}
                        disabled={reordering}
                      >
                        删除
                      </button>
                    </div>
                  ))
                )}
              </div>

              {/* ===== 体检面板（放在音源列表下方，不遮挡音源）=====
                  只有一份日志：
                    - 体检中：显示实时滚动的进度行
                    - 跑完：同一块区域改为显示结果明细（不再重复出现第二份日志） */}
              {(probing ||
                probeLiveLines.length > 0 ||
                (probeReport && probeReport.sources.length > 0)) && (
                <div className="probe-panel">
                  <div className="probe-head">
                    <strong>{probing ? "体检中…" : "体检日志"}</strong>
                    <span className="hint">
                      {probing
                        ? "正在逐个音源实测，日志实时刷新"
                        : probeReport?.probedAt
                          ? new Date(probeReport.probedAt).toLocaleString("zh-CN")
                          : ""}
                    </span>
                    {!probing && probeReport && (
                      <span className="probe-summary">
                        正常 {probeReport.sources.filter((s) => s.status === "ok").length}
                        {" · "}不稳定 {probeReport.sources.filter((s) => s.status === "flaky").length}
                        {" · "}失活 {probeReport.sources.filter((s) => s.status === "dead").length}
                      </span>
                    )}
                  </div>

                  {/* 体检中：实时滚动日志；跑完：切换为「测速排行图」。
                      两者互斥，只显示其中一个，不会出现两份内容。 */}
                  {!probing && probeReport && probeReport.sources.length > 0 ? (
                    <>
                      {/* ===== 测速排行图 =====
                          按速度排序，每行：排名 / 名称 / 状态徽标 / 耗时 / 速度条 / 详情 */}
                      <div className="probe-list">
                        {probeReport.sources.map((s, i) => {
                          const dead = s.status === "dead";
                          const flaky = s.status === "flaky";
                          const maxSpeed = Math.max(
                            ...probeReport.sources.map((x) => x.speedMs || 0),
                            1,
                          );
                          const barPct =
                            s.speedMs != null
                              ? Math.max(6, Math.round((s.speedMs / maxSpeed) * 100))
                              : 0;
                          return (
                            <div
                              key={`rank-${s.name}`}
                              className={`probe-row ${dead ? "dead" : flaky ? "flaky" : "ok"}`}
                            >
                              <span className="probe-rank">{i + 1}</span>
                              <span className="probe-name" title={s.name}>
                                {s.name}
                              </span>
                              <span className={`probe-badge ${s.status}`}>
                                {dead ? "失活" : flaky ? "不稳定" : "正常"}
                              </span>
                              <span className="probe-speed">
                                {s.speedMs != null ? `${s.speedMs} ms` : "—"}
                              </span>
                              <span className="probe-bar" aria-hidden>
                                <i
                                  className={`probe-bar-fill ${s.status}`}
                                  style={{ width: `${barPct}%` }}
                                />
                              </span>
                              <span className="probe-detail" title={s.error}>
                                {dead ? s.error || "取不到链接" : `${s.okCount}/${s.total} 成功`}
                              </span>
                            </div>
                          );
                        })}
                      </div>
                      <div className="probe-foot">
                        体检时间：
                        {probeReport.probedAt
                          ? new Date(probeReport.probedAt).toLocaleString("zh-CN")
                          : "未知"}
                        ｜探测样本：晴天、夜曲（酷我）+ 晴天（网易云）
                      </div>
                    </>
                  ) : (
                    <div className="probe-log" ref={probeLogRef}>
                      {probeLiveLines.length === 0 ? (
                        <div className="probe-log-line">
                          <span className="probe-log-detail">正在启动引擎…</span>
                        </div>
                      ) : (
                        probeLiveLines.map((text, i) => {
                          const isDead = text.includes("失活");
                          const isFlaky = text.includes("不稳定");
                          const cls = isDead ? "dead" : isFlaky ? "flaky" : "ok";
                          return (
                            <div key={`live-${i}`} className={`probe-log-line ${cls}`}>
                              <span className="probe-log-detail">{text}</span>
                            </div>
                          );
                        })
                      )}
                    </div>
                  )}

                  {/* 失活音源：勾选后可批量删除 */}
                  {!probing && probeReport && deadPicked.size > 0 && (
                    <div className="probe-actions">
                      <span className="hint">已勾选 {deadPicked.size} 个失活音源</span>
                      <button className="danger-btn" onClick={deletePickedSources}>
                        删除勾选的音源
                      </button>
                      <button className="ghost-btn" onClick={() => setDeadPicked(new Set())}>
                        取消勾选
                      </button>
                    </div>
                  )}

                  {/* 按体检结果一键排序：稳定性优先、同档内按速度 */}
                  {!probing && probeReport && probeReport.sources.some((s) => s.status !== "dead") && (
                    <div className="probe-sort-row">
                      <button
                        className="primary-btn small"
                        onClick={applyProbeOrder}
                        disabled={reordering}
                        title="按「稳定优先、同档内按速度」重排音源优先级，并保存生效"
                      >
                        {reordering ? "排序中…" : "按体检结果排序"}
                      </button>
                      <span className="hint">
                        稳定优先，同档内速度快的靠前；失活的排最后。
                        排序结果会保存为实际下载时的音源尝试顺序。
                      </span>
                    </div>
                  )}


                  {/* 勾选框：跑完后对每个失活音源显示，让用户决定删不删 */}
                  {!probing && probeReport && probeReport.sources.some((s) => s.status === "dead") && (
                    <div className="probe-pick-row">
                      <span className="hint">要删除哪些失活音源？勾选后点上方「删除勾选的音源」</span>
                      {probeReport.sources
                        .filter((s) => s.status === "dead")
                        .map((s) => (
                          <label key={`pick-${s.name}`} className="probe-pick">
                            <input
                              type="checkbox"
                              checked={deadPicked.has(s.name)}
                              onChange={(e) => {
                                setDeadPicked((prev) => {
                                  const next = new Set(prev);
                                  if (e.target.checked) next.add(s.name);
                                  else next.delete(s.name);
                                  return next;
                                });
                              }}
                            />
                            {s.name}
                          </label>
                        ))}
                    </div>
                  )}
                </div>
              )}
            </section>
          )}

          {/* ==================== 设置 ==================== */}
          {tab === "settings" && (
            <section className="panel">
              <div className="settings-grid">
                <label className="field">
                  <span>Python 可执行路径（默认使用内置引擎，一般无需填写）</span>
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
                  <div className="stat-num">{sourceFiles.length}</div>
                  <div className="stat-label">已导入音源</div>
                </div>
                <div className="stat">
                  <div className="stat-num">{okCount}</div>
                  <div className="stat-label">成功条目</div>
                </div>
              </div>
              <p className="hint">
                本应用复用已验证的 Python 下载引擎：多源搜索桥接、下载内容音质校验、
                版本词与歌手校验等能力均保留。下载目录留空时使用引擎默认目录。
              </p>
            </section>
          )}

          {/* ==================== 关于 ==================== */}
          {tab === "about" && (
            <section className="panel">
              <div className="about-hero">
                <div className="about-avatar-wrap">
                  <img
                    className="about-avatar"
                    src="/avatar/author-460.jpg"
                    alt="作者头像"
                    width={96}
                    height={96}
                    loading="lazy"
                  />
                  <BrandMark size={34} className="about-avatar-badge" />
                </div>
                <div>
                  <h3>落雪下载</h3>
                  <p className="hint">
                    读取落雪音乐歌单，多音源批量下载无损音乐。个人学习与研究用途。
                  </p>
                  <span className="about-version">v{appVersion}</span>
                </div>
              </div>

              {/* ===== Star 引导 ===== */}
              <div className="star-card">
                <div className="star-emoji">⭐</div>
                <div className="star-body">
                  <h4>Star 滞销，帮帮我们</h4>
                  <p>
                    这个项目全靠业余时间用爱发电。如果它帮你省下了下歌的时间，
                    点个 <strong>Star</strong> 就是最实在的支持——
                    不花钱，但能让更多人看到它，也是我继续维护的动力。
                  </p>
                  <div className="star-actions">
                    <button
                      className="primary-btn star-btn"
                      onClick={() => openExternal(AUTHOR.star)}
                    >
                      ⭐ 去 GitHub 点个 Star
                    </button>
                    <span className="hint">
                      已经点过了？那也谢谢你，欢迎推荐给同样在下歌的朋友 😄
                    </span>
                  </div>
                </div>
              </div>

              <div className="about-grid">
                <div className="about-card">
                  <div className="about-card-title">作者</div>
                  <div className="about-card-body">{AUTHOR.name}</div>
                </div>
                <div className="about-card">
                  <div className="about-card-title">QQ</div>
                  <div className="about-card-body about-row">
                    <span>{AUTHOR.qq}</span>
                    <button
                      className="icon-btn"
                      title="复制 QQ 号"
                      onClick={() => copyText(AUTHOR.qq, "QQ 号已复制")}
                    >
                      <CopyIcon size={15} />
                    </button>
                  </div>
                </div>
                <div className="about-card wide">
                  <div className="about-card-title">反馈邮箱</div>
                  <div className="about-card-body about-row">
                    <a href={`mailto:${AUTHOR.email}`} className="about-link">
                      {AUTHOR.email}
                    </a>
                    <button
                      className="icon-btn"
                      title="复制邮箱"
                      onClick={() => copyText(AUTHOR.email, "邮箱已复制")}
                    >
                      <CopyIcon size={15} />
                    </button>
                  </div>
                </div>
              </div>

              <div className="about-feedback">
                <p>
                  遇到问题或有建议，欢迎通过<strong>邮箱</strong>或
                  <strong>GitHub Issues</strong> 反馈。提交问题时请附上：操作步骤、
                  日志区的报错内容、以及使用的音源名称。
                </p>
                <div className="about-actions">
                  <button
                    className="primary-btn"
                    onClick={() => openExternal(AUTHOR.issues)}
                  >
                    <ExternalLinkIcon size={15} /> 打开 GitHub Issues
                  </button>
                  <button
                    className="ghost-btn"
                    onClick={() => openExternal(`mailto:${AUTHOR.email}`)}
                  >
                    发送邮件
                  </button>
                  <button className="ghost-btn" onClick={() => openExternal(AUTHOR.repo)}>
                    项目仓库
                  </button>
                  <button
                    className="ghost-btn"
                    onClick={() => checkForUpdate(false)}
                    disabled={checkingUpdate}
                  >
                    {checkingUpdate ? "检查中…" : "检查更新"}
                  </button>
                </div>
              </div>

              <p className="hint about-legal">
                本工具不包含任何音乐内容，也不分发第三方音源脚本。请自行确认所用音源的
                授权与合规性，下载内容仅限个人试听，请勿传播或用于商业用途，并支持正版。
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
    search: "搜索单曲",
    download: "下载歌曲",
    export: "导出歌单",
    sources: "音源",
    settings: "设置",
    about: "关于",
  };
  return map[tab];
}

/**
 * 日志区。
 *
 * 用 memo 包一层：下载过程中 `progress` 事件每几百毫秒就来一次，
 * 每次都会让 App 重渲染。若不隔离，这里最多 800 个日志节点会被
 * 重新 diff 一遍（`logs` 引用没变也照算），纯属浪费。
 * 只有 `logs` 真正变化时才需要重画。
 */
const LogView = memo(function LogView({
  logs,
  logEndRef,
}: {
  logs: LogLine[];
  logEndRef: React.RefObject<HTMLDivElement | null>;
}) {
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
});

export default App;
