#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lx_music_downloader.py - 落雪音乐歌单一键下载器

功能：
  1. 读取落雪音乐本地数据库（lx.data.db）或导出歌单（.lxmc/.json）
  2. 通过 Node 桥接服务（lx_url_server.js）+ 音源脚本实时获取播放链接
  3. 并发下载所有歌曲到指定目录，每首都过四重校验

单首歌的下载管线（process_song，由外到内逐级兜底）：
  1. 已存在检查   下载目录里已有“歌手 - 歌名”文件则直接跳过
  2. 原源直查     用数据库记录的平台 ID 请求音源取链接；
                  开启降级时按 高→低 音质逐档尝试（底线 320k，绝不降其下）
  3. 替代源直查   数据库里同名歌曲的其他平台版本（tx/wy/kw/kg 顺序，
                  模糊匹配“歌名（版本后缀）”这类条目）逐个尝试
  4. 按名搜索兜底 不带平台 ID，用“歌手+歌名”跨平台搜索，
                  返回多个候选链接逐个下载实测
  5. 四重校验     每个候选下载后必须全部通过才算成功：
                    时长校验（±15s，防下错歌）
                    版本拦截（伴奏/翻唱/卡拉ok/remix 等关键词）
                    歌手校验（artist 标签与预期一致，防翻唱冒充原唱）
                    音质校验（按文件内容探测：真无损或码率≥320k）
  6. 全部失败的歌曲进入二次重试（倒序音源再来一遍），仍失败写入 failed.txt

用法：
  python lx_music_downloader.py                      # 交互式选择
  python lx_music_downloader.py --list               # 列出歌单
  python lx_music_downloader.py --playlist <id>      # 下载指定歌单
  python lx_music_downloader.py --playlist <id> --quality flac --dir D:/Music --workers 4
  python lx_music_downloader.py --retry-failed D:/Music   # 只补下该目录 failed.txt 里的歌
"""
import argparse
import ipaddress
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import queue
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

# 强制 stdout/stderr 以 UTF-8 输出（errors='replace'）：中文 Windows 下默认用 GBK，
# 打印含 GBK 外字符（如日文「・」U+30FB）会抛 UnicodeEncodeError 使整个程序崩溃。
# 输出由 Tauri 前端优先按 UTF-8 解码（decode_line），强制 UTF-8 不会产生乱码。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

# ============ 配置 ============
DEFAULT_DB = os.path.join(os.environ.get('APPDATA', ''), 'lx-music-desktop', 'LxDatas', 'lx.data.db')
# 冻结后的 EXE：资源从临时目录读取，用户数据仍写在 EXE 所在目录。
RESOURCE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
URL_SERVER = os.path.join(RESOURCE_DIR, 'lx_url_server.js')
# 冻结版默认下载到用户「音乐」文件夹（引擎 EXE 藏在 AppData，下载不该去那里）；
# 开发模式仍下载到脚本目录下的 downloads/。
if getattr(sys, 'frozen', False):
    DOWNLOAD_DIR = os.path.join(os.path.expanduser('~'), 'Music', '落雪音乐下载')
else:
    DOWNLOAD_DIR = os.path.join(APP_DIR, 'downloads')
BUNDLED_NODE = os.path.join(RESOURCE_DIR, 'node', 'node.exe')

# 用户音源目录：音源脚本由用户自行导入，存到 APPDATA 下（不随程序/仓库分发），
# GUI「音源」页的导入/删除也操作此目录；引擎默认从这里加载音源。
USER_SOURCES_DIR = os.path.join(os.environ.get('APPDATA', APP_DIR), 'lx-downloader-sources')

# Node 权限模型（--permission）可用性缓存。
# None = 尚未探测；True = 可用；False = 该环境不支持，已回退到无隔离模式。
# 音源脚本是第三方代码且 vm 可被逃逸，--permission 是进程级的兜底防线。
_NODE_PERMISSION_SUPPORTED = None

# 传给 Node 音源进程的环境变量白名单（**必须大写**：Windows 环境变量名不区分大小写，
# 用原样大小写比较会漏掉 SYSTEMROOT 这类以全大写存在的变量，导致 Node 起不来）。
#
# 背景：音源脚本可通过 vm 逃逸读到 process.env。若继承全部环境变量，
# 用户的各类 API key / 许可证路径 / 会话信息会直接暴露给第三方脚本。
# 这份白名单只保留：Node 运行必需的系统变量 + 音源可能用到的代理/TLS/时区设置。
# 实测（71 项 → 35 项）音源取链功能完全正常。
_SANDBOX_ENV_KEYS = frozenset(s.upper() for s in (
    # —— Node / Windows 运行必需 ——
    'PATH', 'PATHEXT', 'COMSPEC', 'OS', 'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE',
    'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP', 'USERPROFILE',
    'HOMEDRIVE', 'HOMEPATH', 'USERNAME', 'USERDOMAIN', 'COMPUTERNAME',
    'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE', 'PROCESSOR_IDENTIFIER',
    'PROCESSOR_LEVEL', 'PROCESSOR_REVISION',
    'PROGRAMDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432',
    'COMMONPROGRAMFILES', 'COMMONPROGRAMFILES(X86)', 'COMMONPROGRAMW6432',
    'ALLUSERSPROFILE', 'PUBLIC', 'SESSIONNAME', 'LOGONSERVER',
    # —— 音源可能依赖的网络/区域设置 ——
    'TZ', 'LANG', 'NO_COLOR',
    'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY',
    'http_proxy', 'https_proxy', 'no_proxy',
    'NODE_TLS_REJECT_UNAUTHORIZED',
))


def build_sandbox_env():
    """构造传给 Node 音源进程的最小环境变量集合。

    只保留白名单内的变量，其余（API key、许可证路径、会话 ID 等）一律不传。
    保留变量名原有大小写，避免 Windows 上某些程序对大小写敏感。
    """
    return {k: v for k, v in os.environ.items() if k.upper() in _SANDBOX_ENV_KEYS}

# 落雪音乐数据库候选路径：官方版 / 便携版 / 不同 fork / 历史版本，
# 保证不同安装方式的用户都能自动识别到歌单。
DB_PATH_CANDIDATES = [
    os.path.join(os.environ.get('APPDATA', ''), 'lx-music-desktop', 'LxDatas', 'lx.data.db'),
    os.path.join(os.environ.get('APPDATA', ''), 'lx-music-desktop', 'LxDatas', 'lx.data.db.0'),
    os.path.join(os.environ.get('APPDATA', ''), 'lx-music-desktop', 'lx.data.db'),
    os.path.join(os.environ.get('APPDATA', ''), 'lx-music-desktop', 'LxDatas', 'user_api.json'),
    os.path.join(os.environ.get('APPDATA', ''), '洛雪音乐', 'LxDatas', 'lx.data.db'),
    os.path.join(os.environ.get('LOCALAPPDATA', ''), 'lx-music-desktop', 'LxDatas', 'lx.data.db'),
    os.path.join(os.environ.get('LOCALAPPDATA', ''), 'lx-music-desktop', 'lx.data.db'),
    r'C:\lx-music\LxDatas\lx.data.db',
    r'D:\lx-music\LxDatas\lx.data.db',
    r'D:\落雪音乐\LxDatas\lx.data.db',
]


def detect_database(explicit=None):
    """自动探测落雪音乐数据库路径。
    优先使用显式指定；否则依次探测候选路径（含便携版、fork、扩展名 .db.0 等）。
    返回存在的数据库路径；找不到时返回 None 并给出候选位置提示。"""
    if explicit:
        p = str(explicit).strip()
        if p.lower().endswith(('.lxmc', '.json', '.txt', '.csv')):
            return p  # 导入文件交给调用方处理
        if os.path.exists(p):
            return p
    for p in DB_PATH_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return None

QUALITY_OPTIONS = {
    '1': ('128k', '标准'),
    '2': ('192k', '较高'),
    '3': ('320k', '高品质'),
    '4': ('flac', '无损'),
    '5': ('flac24bit', 'Hi-Res'),
}
QUALITY_LABELS = {value[0]: value[1] for value in QUALITY_OPTIONS.values()}

# 单首歌的总时间预算（秒）。超预算就放弃这首歌并记入 failed.txt。
#
# 依据：正常歌曲从取链到落盘约 10-60s；而失败歌曲会走
# 「多个候选源 × 多个音质档 × 每源 20s 超时」+「按名搜索 30s 预算」，
# 实测累积到 200-400s。这段时间足够其它 5-8 首正常歌曲完成，
# 让一首失败歌独占并发位是纯浪费。
SONG_TIME_BUDGET = 150

DOWNLOAD_STOP_EVENT = threading.Event()
# GUI 优雅停止：Rust 侧写入该信号文件，本进程轮询到后走与 Ctrl+C 相同的收尾流程
# （停止派发新任务 → 在途任务落盘 → 写 failed.txt），避免被直接强杀而留下 .part。
STOP_FILE = None


def watch_stop_file(path, interval=0.5):
    """后台线程：轮询停止信号文件，出现即置位 DOWNLOAD_STOP_EVENT 并删除该文件。"""
    global STOP_FILE
    STOP_FILE = str(path)
    stop_path = Path(STOP_FILE)

    def _loop():
        while not DOWNLOAD_STOP_EVENT.is_set():
            try:
                if stop_path.exists():
                    safe_unlink(stop_path)
                    request_stop()
                    return
            except OSError:
                pass
            time.sleep(interval)

    t = threading.Thread(target=_loop, name='stop-file-watcher', daemon=True)
    t.start()
    return t


# ============ 歌单导入（.lxmc / JSON） ============
# 洛雪音乐导出的歌单文件 .lxmc 本质是 gzip 压缩的 JSON：
#   {"type":"playList_v2","data":[{"id","name","list":[{id,name,singer,source,interval,meta}]}]}
# 也兼容未压缩的 JSON 备份（数组或 {playlist:[...]} 对象）。


def load_lxmc_playlists(path):
    """读取落雪导出的歌单文件（.lxmc 或 .json），返回 [{'id','name','cnt'}]。"""
    path = str(path)
    raw = None
    with open(path, 'rb') as f:
        head = f.read(4)
        f.seek(0)
        if head[:2] == b'\x1f\x8b':  # gzip magic
            import gzip
            raw = gzip.decompress(f.read())
        else:
            raw = f.read()
    text = raw.decode('utf-8', errors='replace')
    data = json.loads(text)
    # 兼容几种常见结构
    items = []
    if isinstance(data, dict):
        items = data.get('data') or data.get('playList') or data.get('playlist') or data.get('list')
    elif isinstance(data, list):
        items = data
    if not isinstance(items, list):
        raise RuntimeError(f'无法解析歌单文件: {path}')
    result = []
    for pl in items:
        if not isinstance(pl, dict):
            continue
        song_list = pl.get('list') if isinstance(pl.get('list'), list) else []
        result.append({'id': str(pl.get('id', '')), 'name': pl.get('name') or '未命名歌单', 'cnt': len(song_list)})
    return result


def load_lxmc_songs(path, playlist_id):
    """从落雪导出歌单文件中取指定歌单的全部歌曲，格式与 get_songs 返回一致。"""
    path = str(path)
    with open(path, 'rb') as f:
        head = f.read(4)
        f.seek(0)
        import gzip
        raw = gzip.decompress(f.read()) if head[:2] == b'\x1f\x8b' else f.read()
    data = json.loads(raw.decode('utf-8', errors='replace'))
    items = data.get('data') if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise RuntimeError('无法解析歌单文件')
    for pl in items:
        if not isinstance(pl, dict):
            continue
        if str(pl.get('id', '')) == str(playlist_id):
            songs = []
            for s in pl.get('list', []):
                if not isinstance(s, dict):
                    continue
                songs.append({
                    'id': str(s.get('id', '')),
                    'name': s.get('name', ''),
                    'singer': s.get('singer', ''),
                    'source': s.get('source', ''),
                    'interval': s.get('interval', ''),
                    'meta': json.dumps(s.get('meta') or {}, ensure_ascii=False),
                })
            return songs
    return []


def is_lxmc_file(path):
    """判断路径是否为落雪导出的歌单文件（.lxmc/.json）"""
    return str(path).lower().endswith(('.lxmc', '.json'))


def find_node():
    """查找 node 可执行文件"""
    candidates = [
        BUNDLED_NODE,
        r'C:\Program Files\nodejs\node.exe',
        'node',  # PATH
    ]
    for c in candidates:
        try:
            r = subprocess.run([c, '--version'], capture_output=True, timeout=10)
            if r.returncode == 0:
                return c
        except Exception:
            continue
    return None


# ============ Node 音源桥接客户端 ============
# 与 lx_url_server.js 的 JSON-RPC 管道封装：每个下载线程一个独立实例，
# 避免全局锁把取链接请求串行化（见 run() 里的 thread-local 池）。


class UrlServer:
    """与 Node 音源桥接服务通信（常驻子进程）"""

    def __init__(self, node_exe, api_file=None, api_dir=None):
        self.proc = None
        self.lock = threading.Lock()
        self.counter = 0
        self.node_exe = node_exe
        self.api_file = api_file
        self.api_dir = api_dir

    def start(self):
        global _NODE_PERMISSION_SUPPORTED
        cmd = [self.node_exe]

        # ===== 安全隔离：Node 权限模型 =====
        #
        # 音源脚本是用户从网络下载的第三方 JS，由 vm.runInContext 执行。
        # 但 vm **不是安全边界**：脚本可用 `this.constructor.constructor(...)`
        # 这类方式逃逸拿到宿主 process（实测 4/5 种逃逸方式有效），
        # 随后就能读环境变量、读写任意文件、启动子进程。
        #
        # Node 22 的 --permission 能从进程层面兜住这个逃逸：
        # 实测开启后，即使 vm 已逃逸，写文件与 spawn 仍被拒绝（ERR_ACCESS_DENIED）。
        # 这里只授予「读音源目录 + 读本脚本所在目录（node 需要读 lx_url_server.js）」，
        # 写权限**只开一个文件**：音源健康度 .health.json（见下方说明）。
        #
        # 若运行环境不支持该参数（Node < 20 或某些打包环境），自动回退到
        # 无隔离模式并打印警告，绝不因为加固而导致功能不可用。
        if _NODE_PERMISSION_SUPPORTED is not False:
            read_paths = []
            if self.api_file:
                read_paths.append(os.path.dirname(os.path.abspath(self.api_file)))
            if self.api_dir:
                read_paths.append(os.path.abspath(self.api_dir))
            read_paths.append(os.path.dirname(os.path.abspath(URL_SERVER)))
            perm_args = []
            for p in dict.fromkeys(read_paths):  # 去重且保序
                if p and os.path.isdir(p):
                    perm_args.append(f'--allow-fs-read={p}')

            # 音源健康度需要跨进程持久化，必须能写 .health.json。
            # 关键：只授权这一个**文件**，而不是整个目录 ——
            # 实测 `--allow-fs-write=<目录>/.health.json` 时，
            # 写 .health.json 成功，写同目录下的其它文件仍被 ERR_ACCESS_DENIED 拒绝，
            # 因此不会给音源脚本留下「往音源目录丢恶意 .js」的能力。
            health_dir = os.path.abspath(self.api_dir) if self.api_dir else (
                os.path.dirname(os.path.abspath(self.api_file)) if self.api_file else None
            )
            if health_dir and os.path.isdir(health_dir):
                perm_args.append(f'--allow-fs-write={os.path.join(health_dir, ".health.json")}')

            if perm_args:
                cmd += ['--permission'] + perm_args

        cmd.append(URL_SERVER)
        if self.api_file:
            cmd += ['--api', self.api_file]
        elif self.api_dir:
            cmd += ['--api-dir', self.api_dir]
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            # 只传白名单环境变量：音源脚本可逃逸 vm 拿到 process.env，
            # 继承全部 71 项会把用户的各类 API key / 许可证路径直接暴露给第三方脚本。
            # 白名单保留 Node 运行必需 + 音源可能用到的代理/TLS/时区设置（实测功能不受影响）。
            env=build_sandbox_env(),
        )
        # 等待 ready 行
        ready = self.proc.stdout.readline()
        if '"ready"' not in ready:
            err = self.proc.stderr.read() if self.proc.stderr else ''
            # 不支持 --permission 的环境：去掉该参数重试一次，保证功能可用
            if _NODE_PERMISSION_SUPPORTED is not False and any(
                    str(a).startswith('--permission') or str(a).startswith('--allow-fs-read')
                    for a in cmd):
                _NODE_PERMISSION_SUPPORTED = False
                try:
                    if self.proc.poll() is None:
                        self.proc.kill()
                except Exception:
                    pass
                # 必须写 stderr：stdout 是 JSON 协议通道（--search/--list-songs），
                # 往 stdout 打警告会污染上层解析，正是之前「返回了非 JSON 内容」的成因之一。
                print('[警告] 当前 Node 不支持 --permission，已回退到无隔离模式；'
                      '音源脚本将以完全权限运行，请只导入可信来源。',
                      file=sys.stderr, flush=True)
                return self.start()
            raise RuntimeError(f'音源服务启动失败: {ready} {err}')
        # 后台读取 stderr 以防阻塞
        def _drain():
            try:
                while True:
                    line = self.proc.stderr.readline()
                    if not line:
                        break
            except Exception:
                pass
        t = threading.Thread(target=_drain, daemon=True)
        t.start()
        return self

    def request(self, cmd, on_event=None, **kwargs):
        """发一条指令并等待对应 id 的响应。

        on_event：可选的进度回调。Node 侧在执行长任务（如音源体检）时，
        会先输出若干行 `{"event":"probeProgress", ...}` 再输出最终响应。
        这些中间行不属于响应，交给 on_event 处理（用于实时打印进度），
        不会污染最终返回值。
        """
        with self.lock:
            self.counter += 1
            my_id = self.counter
            msg = {'id': my_id, 'cmd': cmd, **kwargs}
            self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + '\n')
            self.proc.stdin.flush()
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    raise RuntimeError('音源服务已退出')
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    # 非 JSON 行（如音源脚本的 console 输出）直接忽略
                    continue
                # 进度事件：交给回调，继续等真正的响应
                if 'event' in obj and 'id' not in obj:
                    if on_event:
                        try:
                            on_event(line)
                        except Exception:
                            pass
                    continue
                # 只接受本次请求 id 的响应，避免串号
                if obj.get('id') != my_id:
                    continue
                return obj

    def get_urls(self, source, music_info, quality):
        """获取播放链接候选列表（多音质），对临时网络错误/平台限流自动退避重试。"""
        last_error = None
        for attempt in range(3):
            try:
                resp = self.request('url', source=source, musicInfo=music_info, quality=quality)
                if resp.get('ok'):
                    return resp.get('candidates', [])
                last_error = RuntimeError(resp.get('error', '未知错误'))
            except Exception as exc:
                last_error = exc
            # 平台限流/网络抖动通常几十秒内恢复；退避重试比立即判死更能命中。
            time.sleep(3 if attempt == 0 else 8)
        raise RuntimeError(str(last_error or '未知错误'))

    def search_by_name_list(self, name, singer, interval=None):
        """按歌手+歌名搜索获取候选链接列表（兜底）。返回 [{url, quality, via}]。
        候选按 URL 特征排序，但 URL 特征可能被代理型链接误导（标无损却返回 48k），
        因此调用方必须逐个下载并校验真实内容后再采纳。"""
        resp = self.request('searchByName', name=name, singer=singer, interval=interval)
        if resp.get('ok'):
            return resp.get('candidates', []) or []
        raise RuntimeError(resp.get('error', '未知错误'))

    def search_songs(self, keyword, limit=30):
        """单曲搜索：返回歌曲元数据列表（不含直链）。
        GUI「搜索」页用它列出候选，用户点某首时再走 get_urls 取链。"""
        resp = self.request('searchSongs', keyword=keyword, limit=limit)
        if resp.get('ok'):
            return resp.get('songs', []) or []
        raise RuntimeError(resp.get('error', '未知错误'))

    def close(self):
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass
            # terminate 只是发信号，不等待就可能留下僵尸进程；
            # 大批量下载会起很多 Node 实例（每线程一个），必须真正回收。
            try:
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=2)
                except Exception:
                    pass
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                try:
                    if stream:
                        stream.close()
                except Exception:
                    pass
            self.proc = None


# ============ 文件名归一化与模糊匹配 ============


def sanitize_filename(name):
    """清理文件名非法字符，并防止 `..` 造成目录穿越。

    歌名/歌手来自第三方音源脚本，完全不可信。虽然已把路径分隔符替换成 `_`，
    但若结果恰好是 `..`（或含 `..` 段），`download_dir / name` 仍可能跳出目录。
    这里额外做：折叠连续点、去掉纯点段、限制长度（Windows 上限 255，留余量）。
    """
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', name)
    # 把「两个及以上的连续点」压成一个点，避免 ".." 与 "..." 之类的目录语义
    name = re.sub(r'\.{2,}', '.', name)
    name = name.strip(' .')
    if not name or name in ('.', '..'):
        return 'untitled'
    # 长度限制：按字符数截断（不同文件系统按字节，但 120 字符对 UTF-8 也安全）
    if len(name) > 120:
        name = name[:120].rstrip(' .')
    return name or 'untitled'


def safe_join(base_dir, filename):
    """把 filename 安全地拼到 base_dir 下，确保结果不越界。

    双重保险：即使 sanitize_filename 有疏漏，这里也会拒绝解析后跑到外面的路径。
    """
    base = Path(base_dir).resolve()
    candidate = (base / filename).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        raise RuntimeError(f'文件名越界，已拒绝: {filename!r}')
    return candidate


def strict_song_key(s):
    """严格匹配键：只做「首尾空白 + 连续空白折叠 + 大小写 + 全半角」归一化。

    **绝不删除括号内容、标点或任何字符**。原因（真实库 773 首里有 35 组同键冲突）：
      - 「出山」 vs 「出山 (DJ版)」
      - 「高阶萌妹成长指南」 vs 「高阶萌妹成长指南 (消音伴奏)」
      - 「萌妹进行曲」 vs 「萌妹进行曲（高阶萌妹成长指南）」
    若按「去掉括号再比」，这些会被判成同一首，正是下错歌的根源。
    """
    if not s:
        return ''
    s = str(s)
    # 全角 ASCII（！ 到 ～）→ 半角，避免「（」与「(」被判成不同
    s = ''.join(
        chr(ord(ch) - 0xFEE0) if 0xFF01 <= ord(ch) <= 0xFF5E else ch
        for ch in s
    )
    s = s.replace('\u3000', ' ')          # 全角空格 → 半角
    s = re.sub(r'\s+', ' ', s).strip()    # 折叠空白
    return s.casefold()


def strict_song_key_match(a, b):
    """严格比较：归一化后必须**完全相等**。空值一律视为不匹配。

    旧实现「空即放行」会让没有标题的候选直接通过校验，这里一并收紧。
    """
    ka = strict_song_key(a)
    kb = strict_song_key(b)
    if not ka or not kb:
        return False
    return ka == kb


def norm_song_key(s):
    """宽松归一化（**已无调用方，仅保留给外部脚本/测试参考**）。

    会去掉括号内容与标点，是「不可逆的多对一映射」——「出山」与「出山 (DJ版)」
    会塌缩成同一个键。判断「是不是同一首歌」一律用 strict_song_key。
    """
    if not s:
        return ''
    s = re.sub(r'\([^)]*\)', '', str(s))   # 半角括号内容
    s = re.sub(r'（[^）]*）', '', str(s))  # 全角括号内容
    s = re.sub(r'\s+', '', s)
    s = re.sub(r'[!-/:-@\[-`{-~]', '', s)
    return s.lower()


def norm_song_key_match(a, b):
    """宽松比较（**已无调用方**）。

    保留它只是为了让 `analyze_quality.py` 等辅助脚本仍可导入；
    主流程已全部改用 `strict_song_key_match`。
    """
    na = norm_song_key(a)
    nb = norm_song_key(b)
    if not na or not nb:
        return True
    if na == nb:
        return True
    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    return long.startswith(short)


# ============ 失败清单 failed.txt（v2 格式：人类可读 + 可解析） ============
#
# 旧格式（v1）是给人看的：
#     --- 2026-10-18 15:30 ---
#     周杰伦 - 晴天: 音质不达标(低于 320k)
# 问题是**不可逆地有歧义**：歌名里可能有 `:`，错误原因里也有 `:`，
# 「歌手 - 歌名」里还可能有 ` - `。无论怎么切都无法可靠还原字段，
# 导致「只重试上次失败的歌」这件事做不了。
#
# v2 改为「注释行 + 每首一行 JSON（JSONL）」：
#     # lx-downloader failed list v2
#     # 2026-10-18 15:30
#     {"name":"晴天","singer":"周杰伦","source":"kw","interval":"04:29","error":"音质不达标"}
# 为什么选 JSONL 而不是 TSV：
#   1. JSON 有转义，歌名/错误原因里的 `\t`、换行、引号都不会破坏结构；
#   2. 单行即完整记录，用户用记事本打开仍是一行一首，可读性不输 TSV；
#   3. 引擎已有 json 依赖，解析失败时可逐行跳过，坏一行不影响其它行。
# 注释行（`#` 开头）记录版本与批次时间，既保留「这批是什么时候失败的」信息，
# 又不会干扰解析。
FAILED_FILE_NAME = 'failed.txt'
FAILED_HEADER_V2 = '# lx-downloader failed list v2'
# v1 的时间戳分隔行，例如 `--- 2026-10-18 15:30 ---`
_V1_BATCH_RE = re.compile(r'^-{3,}\s*(.*?)\s*-{3,}$')


def _failed_key(song):
    """失败清单的去重键：歌名 + 歌手（严格归一化）。

    不含 source/interval：同一首歌在不同批次里可能来自不同平台，
    去重时应视为同一首，否则清单里会留下重复项。
    """
    return (strict_song_key(song.get('name')), strict_song_key(song.get('singer')))


# 引擎写 failed.txt 时，错误原因的开头几乎总是这些前缀之一
# （来自 `f'{source}: {异常}'`、`按名搜索: ...`，或直接是校验失败文案）。
# 用它来定位「歌名与错误原因」的分界点，比单纯取第一个 `: ` 更准。
_V1_ERROR_PREFIXES = (
    'kw:', 'wy:', 'tx:', 'kg:', 'mg:', 'local:',
    '按名搜索:', '单曲时间预算耗尽', '无可用来源', '所有音质均失败',
    '下载失败', '音质不达标', '时长校验失败', '歌手不符', '版本不符',
    '获取链接失败', '链接无效',
)


def _parse_v1_line(line):
    """尽力解析 v1 的 `歌手 - 歌名: 错误原因` 行；无法解析时返回 None。

    v1 本身有歧义：歌名里可能有 `: `（如 `AC/DC: Live`），错误原因里也一定有 `: `。
    做法是**从左往右找第一个「右侧像错误原因」的 `: `**：
      - 引擎写的错误来自 `f'{source}: {异常}'` 再用 `; ` 拼接，开头是已知前缀；
      - 因此逐個候选切点检查右半是否以已知错误前缀开头，第一个命中的就是真分界。
      - 全都不是已知前缀时，退回「第一个 `: `」——这样最多截断带冒号的歌名，
        而不会把整段错误文本吃进歌名（那会导致重试时按名搜索必然找不到）。
    左半部分再在**第一个** ` - ` 处切开（歌手极少含 ` - `，歌名可能含）。
    解析不出来的行直接跳过——宁可少重试一首，也不要把错误的歌塞进下载队列。
    """
    text = line.strip()
    if not text:
        return None

    head, error = None, ''
    start = 0
    while True:
        pos = text.find(': ', start)
        if pos <= 0:
            break
        right = text[pos + 2:]
        # 只在「右半部分确实像错误原因」时才认这个切点，
        # 且要求左半部分已经含有 ` - `（即 `歌手 - 歌名` 结构完整）。
        # 否则像 `AC/DC: Live - 某歌手: 音质不达标` 这种「歌手名自带冒号」的行
        # 会在歌手名中间被切开，把歌手和歌名对调——比截断更难排查。
        if right.startswith(_V1_ERROR_PREFIXES) and ' - ' in text[:pos]:
            head, error = text[:pos], right
            break
        start = pos + 1

    if head is None:
        # 没有已知前缀：退回第一个 `: `（见 docstring 的取舍说明）
        pos = text.find(': ')
        if pos > 0:
            head, error = text[:pos], text[pos + 2:]
        elif text.endswith(':'):
            head, error = text[:-1], ''
        else:
            head, error = text, ''

    if ' - ' in head:
        singer, name = head.split(' - ', 1)
    else:
        # 没有歌手时 v1 只写歌名
        singer, name = '', head
    name = name.strip()
    if not name:
        return None
    return {
        'name': name,
        'singer': singer.strip(),
        'source': '',
        'interval': '',
        'error': error.strip(),
    }


def parse_failed_txt(text):
    """解析 failed.txt 内容，返回歌曲字典列表（按出现顺序去重）。

    同时兼容 v2（JSONL）与 v1（`歌手 - 歌名: 原因`）：
      - `#` 开头 → 注释，跳过
      - `{` 开头 → 尝试按 JSON 解析；成功即采纳，失败则退回 v1 解析
        （v1 的歌名理论上可能以 `{` 开头，不能因为首字符就丢弃）
      - `--- ... ---` → v1 批次分隔，跳过
      - 其它 → 按 v1 尽力解析
    任何单行解析失败都只跳过该行，绝不让整份清单报废。
    """
    songs = []
    seen = set()
    for raw in str(text or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#'):
            continue
        if _V1_BATCH_RE.match(line):
            continue
        item = None
        if line.startswith('{'):
            try:
                obj = json.loads(line)
            except Exception:
                obj = None
            if isinstance(obj, dict):
                name = str(obj.get('name') or '').strip()
                if name:
                    item = {
                        'name': name,
                        'singer': str(obj.get('singer') or '').strip(),
                        'source': str(obj.get('source') or '').strip(),
                        'interval': str(obj.get('interval') or '').strip(),
                        'error': str(obj.get('error') or '').strip(),
                    }
        if item is None:
            item = _parse_v1_line(line)
        if not item:
            continue
        key = _failed_key(item)
        if not key[0] or key in seen:
            continue
        seen.add(key)
        songs.append(item)
    return songs


def load_failed_songs(directory):
    """读取 <目录>/failed.txt 并解析成歌曲列表；文件不存在返回空列表。"""
    path = Path(directory) / FAILED_FILE_NAME
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except FileNotFoundError:
        return []
    except OSError as exc:
        print(f'[错误] 读取失败清单失败: {exc}', file=sys.stderr, flush=True)
        return []
    return parse_failed_txt(text)


def failed_to_song(item):
    """把失败清单条目转换成 run_download 认识的歌曲结构。

    关键：**不写 meta**。失败清单里没有原始平台 ID（也不该存，ID 会失效），
    所以重试时走「原源 + 按名搜索兜底」：source 只用于决定先问哪个平台，
    真正的 ID 由 searchByName 现场搜出来，这比拿旧 ID 直查更可靠。
    """
    return {
        'id': '',
        'name': item['name'],
        'singer': item.get('singer', ''),
        'source': item.get('source', ''),
        'interval': item.get('interval', ''),
        'meta': '{}',
    }


def write_failed_file(directory, items):
    """用 v2 格式重写 failed.txt（items 为空则删除该文件）。

    为什么整份重写而不是追加：重试成功后必须把成功的歌**移出**清单，
    否则清单只增不减，用户点「重试」永远在重下已经下好的歌。
    写入用「临时文件 + 替换」，避免写到一半崩溃留下半份清单。
    """
    path = Path(directory) / FAILED_FILE_NAME
    if not items:
        safe_unlink(path)
        return
    lines = [FAILED_HEADER_V2, f"# {time.strftime('%Y-%m-%d %H:%M')}"]
    for item in items:
        record = {
            'name': item.get('name', ''),
            'singer': item.get('singer', ''),
            'source': item.get('source', ''),
            'interval': item.get('interval', ''),
            'error': item.get('error', ''),
        }
        lines.append(json.dumps(record, ensure_ascii=False))
    tmp = path.with_name(path.name + '.tmp')
    try:
        tmp.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        os.replace(tmp, path)
    except OSError as exc:
        safe_unlink(tmp)
        print(f'[警告] 写入失败清单失败: {exc}', file=sys.stderr, flush=True)


# ============ 落雪数据库（只读访问） ============


def _connect_db_ro(db_path):
    """以只读方式打开落雪音乐数据库（只读绝不写入，避免生成 -wal/-shm 及损坏用户数据）。
    注意：落雪桌面版可能正在运行且把数据库置于 WAL 模式，只读打开不会创建 -wal 文件。"""
    return sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)


# 替代源查询缓存：同一进程内同一首歌只查一次库。
# 16 并发下每首歌都开一次「全表拉取 + Python 过滤」会造成明显的数据库争用，
# 缓存后既少开连接也少做重复过滤。key = (db_path, song_id)。
_ALT_SOURCE_CACHE = {}
_ALT_SOURCE_LOCK = threading.Lock()
# 缓存条目上限：超出即清空（该缓存只用于单首歌的重复查询去重，清空无副作用）
_ALT_SOURCE_CACHE_MAX = 800


def get_alternative_sources(db_path, song):
    """查找同曲的其他平台记录（替代源回退）。

    **严格匹配**：歌名与歌手都必须是「归一化后完全相等」（只忽略空白/大小写/全半角）。
    不再把「出山」与「出山 (DJ版)」、「萌妹进行曲」与「萌妹进行曲（高阶萌妹成长指南）」
    当成同一首——真实库里这类同键冲突有 35 组，模糊匹配正是下错歌的主因。

    若来源是导入的 .lxmc/.json 歌单文件（非 SQLite），无数据库可查，返回空列表。
    """
    if is_lxmc_file(str(db_path)):
        return []

    target_name = strict_song_key(song['name'])
    target_singer = strict_song_key(song['singer'])
    if not target_name:
        return []

    cache_key = (str(db_path), str(song.get('id') or ''), target_name, target_singer)
    with _ALT_SOURCE_LOCK:
        cached = _ALT_SOURCE_CACHE.get(cache_key)
    if cached is not None:
        return [dict(r) for r in cached]

    conn = _connect_db_ro(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    # 先用 SQL 缩小候选集：name 的精确/前缀匹配交给索引与 SQL 过滤，
    # 避免每次都把整表拉进 Python。带括号的版本后缀用 LIKE 前缀兜住。
    cur.execute("""
        SELECT id, name, singer, source, interval, meta
        FROM my_list_music_info
        WHERE source != ? AND name != ? AND name LIKE ?
        ORDER BY CASE source WHEN 'tx' THEN 0 WHEN 'wy' THEN 1 WHEN 'kw' THEN 2 WHEN 'kg' THEN 3 ELSE 4 END
    """, (song['source'], song['name'], target_name.replace('%', '').replace('_', '') + '%'))
    cand = [dict(r) for r in cur.fetchall()]
    conn.close()

    rows = []
    seen = set()
    for r in cand:
        # 歌名必须完全一致（含括号内的版本后缀）
        if strict_song_key(r['name']) != target_name:
            continue
        # 歌手：目标歌手为空时不做限制，否则必须完全一致
        if target_singer and strict_song_key(r['singer']) != target_singer:
            continue
        if r['id'] in seen:
            continue
        seen.add(r['id'])
        rows.append(r)

    with _ALT_SOURCE_LOCK:
        # 上限保护：700 首的批量下载会逐首写入缓存，虽然单条很小，
        # 但配合「二次重试」会翻倍。超出上限时直接清空重来——
        # 这个缓存只用于「同一首歌查两次」的去重，清空无正确性影响。
        if len(_ALT_SOURCE_CACHE) >= _ALT_SOURCE_CACHE_MAX:
            _ALT_SOURCE_CACHE.clear()
        _ALT_SOURCE_CACHE[cache_key] = [dict(r) for r in rows]
    return rows


def get_music_info(row):
    """从数据库行构建音源所需的 musicInfo"""
    meta = {}
    try:
        meta = json.loads(row['meta'] or '{}')
    except Exception:
        pass
    info = {
        'name': row['name'],
        'singer': row['singer'],
        'source': row['source'],
        'interval': row.get('interval'),
    }
    # 展开 meta 中的字段
    info.update(meta)
    # 兼容不同字段名（各音源期望不同的 ID 字段）
    sid = info.get('songId')
    if sid is not None:
        if 'id' not in info or info['id'] is None:
            info['id'] = sid
        if 'songmid' not in info or info['songmid'] is None:
            info['songmid'] = str(sid)
    elif 'songmid' in info and ('songId' not in info or info['songId'] is None):
        info['songId'] = info['songmid']

    # 统一剥掉平台前缀：数据库 id 形如 `wy_30070747`、`kw_55219`，
    # 而所有音源要的都是纯 ID（30070747 / 55219）。
    # 少这一步，单曲下载（--song-file 路径）会把带前缀的 id 传给音源导致取链失败。
    for key in ('id', 'songId', 'songmid'):
        val = info.get(key)
        if isinstance(val, str):
            stripped = re.sub(r'^[A-Za-z]{2}_', '', val)
            if stripped != val:
                info[key] = stripped
    if 'hash' not in info and info.get('_qualitys'):
        qs = info.get('_qualitys', {})
        for q in ('320k', '128k', 'flac'):
            if q in qs and qs[q].get('hash'):
                info['hash'] = qs[q]['hash']
                break
    return info


def list_playlists(db_path):
    """列出所有歌单"""
    conn = _connect_db_ro(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    # 默认列表、收藏列表（内置）
    result = []
    for lid, lname in (('default', '默认列表'), ('love', '我的收藏')):
        cur.execute("SELECT COUNT(*) FROM my_list_music_info_order WHERE listId=?", (lid,))
        cnt = cur.fetchone()[0]
        if cnt > 0:
            result.append({'id': lid, 'name': lname, 'cnt': cnt})
    # 自定义歌单
    cur.execute("""
        SELECT ml.id, ml.name,
               (SELECT COUNT(*) FROM my_list_music_info_order mio WHERE mio.listId = ml.id) as cnt
        FROM my_list ml
        ORDER BY ml.position
    """)
    for r in cur.fetchall():
        result.append({'id': r[0], 'name': r[1], 'cnt': r[2]})
    conn.close()
    return result


def get_songs(db_path, playlist_id):
    """获取歌单内所有歌曲"""
    conn = _connect_db_ro(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT mi.id, mi.name, mi.singer, mi.source, mi.interval, mi.meta
        FROM my_list_music_info mi
        JOIN my_list_music_info_order mio ON mio.musicInfoId = mi.id
        WHERE mio.listId = ?
        ORDER BY mio."order"
    """, (playlist_id,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    # 去重（同一首歌可能出现多次）
    seen = set()
    songs = []
    for r in rows:
        if r['id'] in seen:
            continue
        seen.add(r['id'])
        songs.append(r)
    return songs


# ============ 下载引擎（多线程分片 + 断点续传） ============
# 支持 Range 的文件按大小动态分片并行下载再合并；不支持 Range 时退化为
# 单线程流式下载。每次重试前清理残留 .part 分片，失败重试按指数退避。
#
# 分片策略（实测调优）：CDN 常对单连接限速，多连接能显著提速，
# 但分片过多会带来额外握手开销，因此按文件大小分档。


def _choose_parts(total_size, measured_speed=None):
    """按文件大小与实测速度选择分片数。

    实测结论（同一 CDN，30MB FLAC）：
        单连接可能 3s（9.5 MB/s），也可能 13s（0.8 MB/s）——**波动极大**，
        取决于 CDN 节点给这个连接分配多少带宽。
        4 分片稳定在 ~10 MB/s；6 片以上反而 SSL EOF 失败。
    → 策略：先测一小段速度，单连接够快就不分片；慢则切 4 片并行。
      既避免对快节点做无用分片，又能在被限速时靠多连接提速。
    """
    if not total_size or total_size < _MULTIPART_MIN_SIZE:
        return 1
    if measured_speed is None:
        return 4
    # 单连接 < 3 MB/s 视为被限速，值得分片
    return 4 if measured_speed < 3 * 1024 * 1024 else 1


def _probe_speed(url, headers, timeout, sample_bytes=256 * 1024):
    """测一小段（默认 256KB）的下载速度，单位 byte/s。失败返回 None。"""
    try:
        h = dict(headers)
        h['Range'] = f'bytes=0-{sample_bytes - 1}'
        req = Request(url, headers=h)
        t0 = time.time()
        with urlopen(req, timeout=timeout) as resp:
            if getattr(resp, 'status', 200) not in (200, 206):
                return None
            got = 0
            while got < sample_bytes:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                got += len(chunk)
        dt = time.time() - t0
        if dt <= 0 or got <= 0:
            return None
        return got / dt
    except Exception:
        return None


def _is_private_host(host):
    """判断主机名是否指向本机/内网。

    音源脚本是不可信的，若允许它把候选 URL 指向 127.0.0.1 或内网段，
    就能靠「响应耗时/状态码差异」枚举本机与内网服务（SSRF-lite）。
    正常音乐 CDN 不可能落在这些地址上，因此一律拒绝。
    """
    if not host:
        return True
    h = host.strip('[]').lower()
    if h in ('localhost', 'localhost.localdomain', 'ip6-localhost', 'ip6-loopback'):
        return True
    if h.endswith('.local') or h.endswith('.internal') or h.endswith('.localhost'):
        return True
    # 纯 IPv4 / IPv6 字面量
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False  # 普通域名，交还给 DNS
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def is_safe_http_url(url):
    """只接受 http/https 绝对地址，且拒绝本机/内网主机。

    候选 URL 来自第三方音源脚本（不可信）。若不校验协议，
    `file:///C:/...` 会被 urlopen 当本地文件读出来，等同于给音源脚本
    一个「任意读文件」的原语；`ftp://` 等同理。
    """
    if not isinstance(url, str) or not url:
        return False
    try:
        parts = urlsplit(url.strip())
    except Exception:
        return False
    if parts.scheme.lower() not in ('http', 'https'):
        return False
    if not parts.netloc:
        return False
    # 端口一并取出再判 host：netloc 可能形如 host:8080 或 user@host:8080
    try:
        host = parts.hostname
    except Exception:
        return False
    return not _is_private_host(host)


def download_file(url, dest_path, timeout=120, retries=3, parts=None):
    """下载文件：优先多线程分片并行，不支持 Range 时退化为单线程流式；支持断点续传。

    parts=None 时按文件大小自动决定分片数（见 `_choose_parts`）。
    """
    # 纵深防御：即使调用方漏检，这里也拒绝非 http(s) 协议。
    if not is_safe_http_url(url):
        raise RuntimeError(f'不支持的下载协议（仅允许 http/https）: {str(url)[:80]}')
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    last_error = None
    for attempt in range(retries + 1):
        if DOWNLOAD_STOP_EVENT.is_set():
            raise InterruptedError('用户请求停止')
        try:
            headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'audio/*,*/*;q=0.8', 'Referer': url}
            total_size = _probe_content_length(url, headers, timeout)
            if parts is not None:
                n_parts = parts
            else:
                # 先花 ~0.3s 测单连接速度：快就不分片（省掉握手与合并开销），
                # 慢（被 CDN 限速）才切 4 片并行。
                speed = _probe_speed(url, headers, timeout) if total_size else None
                n_parts = _choose_parts(total_size, speed)
            if total_size and n_parts > 1:
                _multipart_download(url, dest_path, total_size, n_parts, headers, timeout)
            else:
                _download_sequential(url, dest_path, timeout, headers)
            return True
        except Exception as e:
            last_error = e
            # 无论中断还是普通失败，都必须清理分片临时文件。
            # 原实现在「停止」分支直接 raise，跳过了下面的清理，
            # 导致中途停止后目录里残留 .part0/.part1… 这些无主文件。
            #
            # 注意：分片文件是 `<目标名>.part0`，即「目标名再拼 .partN」，
            # 因此这里要用字符串拼接而不是 with_suffix —— 后者会把 .part 当扩展名替换掉。
            base = str(dest_path)
            for i in range(16):
                safe_unlink(Path(f'{base}.part{i}'))
            if DOWNLOAD_STOP_EVENT.is_set():
                raise InterruptedError('用户请求停止')
            if attempt < retries:
                time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f'下载失败: {last_error}')


# 分片并行下载门槛：小于此大小的文件用单线程，避免小文件分片开销。
# 1MB 起分片：实测 CDN 单连接常被限速，连 2-3MB 的 320k 文件分片也有收益。
_MULTIPART_MIN_SIZE = 1 * 1024 * 1024


def _probe_content_length(url, headers, timeout):
    """探测服务器是否支持 Range 及返回资源总大小（不支持或无总大小时返回 None）。"""
    try:
        h = dict(headers)
        h['Range'] = 'bytes=0-0'
        req = Request(url, headers=h)
        with urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, 'status', 200)
            ct = (resp.headers.get('Content-Type') or '').lower()
            if status != 206:
                return None
            if any(x in ct for x in ('text/html', 'application/json', 'text/xml')):
                return None
            cr = resp.headers.get('Content-Range') or ''
            m = re.search(r'/(\d+)', cr)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None


def _download_sequential(url, dest_path, timeout, headers):
    """单线程流式下载到 dest_path，支持 Range 断点续传。返回写入总字节数。"""
    try:
        existing = dest_path.stat().st_size if dest_path.exists() else 0
    except Exception:
        existing = 0
    h = dict(headers)
    if existing:
        h['Range'] = f'bytes={existing}-'
    req = Request(url, headers=h)
    with urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, 'status', 200)
        content_type = (resp.headers.get('Content-Type') or '').lower()
        append = existing > 0 and status == 206
        if status not in (200, 206):
            raise RuntimeError(f'HTTP {status}')
        # 不强制 audio/*：拦截明显的错误页/JSON，避免误杀可播放的音频流。
        if any(x in content_type for x in ('text/html', 'application/json', 'text/xml')):
            raise RuntimeError(f'返回错误页: {content_type}')
        mode = 'ab' if append else 'wb'
        total = existing if append else 0
        with open(dest_path, mode) as out:
            while True:
                if DOWNLOAD_STOP_EVENT.is_set():
                    # 停止时把半成品删掉：留着会占空间，更严重的是会被
                    # 「已存在」判断误认为下载成功，导致下次直接跳过。
                    # 顺序下载直接写目标文件（为支持断点续传），所以这里必须清。
                    out.close()
                    safe_unlink(dest_path)
                    raise InterruptedError('用户请求停止')
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
                total += len(chunk)
        if total < 1024:
            safe_unlink(dest_path)
            raise RuntimeError(f'文件过小({total} bytes)，可能是错误页')
    return total


def _download_part(url, start, end, part_path, headers, timeout):
    """下载 [start, end] 字节范围到独立临时分片文件。"""
    h = dict(headers)
    h['Range'] = f'bytes={start}-{end}'
    req = Request(url, headers=h)
    with urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, 'status', 200)
        if status != 206:
            raise RuntimeError(f'分片响应非 206: {status}')
        with open(part_path, 'wb') as out:
            while True:
                if DOWNLOAD_STOP_EVENT.is_set():
                    raise InterruptedError('用户请求停止')
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)


def _multipart_download(url, dest_path, total_size, parts, headers, timeout):
    """多线程分片并行下载，最后按顺序合并到 dest_path。"""
    dest = Path(dest_path)          # 兼容调用方传 str
    base = str(dest)
    part_size = (total_size + parts - 1) // parts
    jobs = []
    for i in range(parts):
        start = i * part_size
        end = min(total_size - 1, start + part_size - 1)
        if start > end:
            break
        # 用字符串拼接而非 with_suffix：dest 可能是 `xxx.part`，
        # with_suffix 会把 `.part` 当成扩展名替换掉，导致分片名与清理逻辑对不上。
        pp = Path(f'{base}.part{i}')
        jobs.append((start, end, pp))
    if not jobs:
        raise RuntimeError('分片切分失败')

    def _worker(job):
        start, end, pp = job
        try:
            _download_part(url, start, end, pp, headers, timeout)
            return None
        except Exception as e:
            return e

    errors = []
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        for f in as_completed([pool.submit(_worker, j) for j in jobs]):
            err = f.result()
            if err:
                errors.append(err)
    if errors:
        for _, _, pp in jobs:
            safe_unlink(pp)
        raise RuntimeError(f'分片下载失败: {errors[0]}')

    # 按顺序合并各分片
    with open(dest, 'wb') as out:
        for _, _, pp in jobs:
            with open(pp, 'rb') as pin:
                while True:
                    chunk = pin.read(1024 * 256)
                    if not chunk:
                        break
                    out.write(chunk)
    for _, _, pp in jobs:
        safe_unlink(pp)
    return total_size


def _print_timing(song, url_cost, dl_cost, total):
    """输出单首歌耗时统计：取链接 / 下载 / 其他 / 合计，用于定位速度瓶颈。"""
    other = max(0.0, total - url_cost - dl_cost)
    print(f'[耗时] {song["name"]} - {song["singer"]}: '
          f'取链接 {url_cost:.1f}s / 下载 {dl_cost:.1f}s / 其他 {other:.1f}s / 合计 {total:.1f}s', flush=True)


def safe_unlink(path):
    """安全删除文件，沙箱环境失败时静默忽略"""
    try:
        if path and Path(path).exists():
            Path(path).unlink(missing_ok=True)
    except Exception:
        try:
            os.remove(path)
        except Exception:
            pass


def song_exists_in_dir(download_dir, singer, name):
    """检查歌单中某首歌是否已下载（按「歌手 - 歌名」**严格**匹配任意扩展名）。

    严格等于「归一化后完全相同」：只忽略空白/大小写/全半角，
    绝不把「晴天」当成「晴天 (Live)」——否则用户要原版时会被错误跳过，
    拿到的是伴奏/Live 版，而且每次重跑都会跳过、永远得不到纠正。
    """
    base_name = sanitize_filename(f"{singer} - {name}")
    try:
        for f in download_dir.iterdir():
            if f.is_file() and f.suffix.lower() in ('.mp3', '.m4a', '.flac', '.aac', '.wav', '.ogg', '.ape'):
                if strict_song_key_match(f.stem, base_name):
                    return f
    except Exception:
        pass
    return None


def download_with_candidates(candidates, dest_base, expected_interval=None, expected_singer=None,
                             requested_quality='320k'):
    """尝试多个候选 URL；每个候选都独立临时文件，避免坏链接污染下一个候选。
    可传 expected_singer 做歌手标签校验，防止翻唱版冒充原唱。
    requested_quality 决定音质校验门槛（请求 128k 就按 128k 验收，请求无损就必须真无损）。
    dest_base 为不含扩展名的目标路径；四重校验通过后按文件内容确定真实扩展名
    再落盘（URL 扩展名不可靠，避免把 FLAC 存成假 .mp3）。
    返回 (音质标签, 实际保存路径)。"""
    errors = []
    part_path = Path(str(dest_base) + '.part')
    for cand in candidates:
        safe_unlink(part_path)
        # 只允许 http(s)：候选 URL 来自第三方音源脚本，若不校验 scheme，
        # `file:///C:/Users/x/.ssh/id_rsa` 会被 urlopen 直接读出来并落成"歌曲文件"。
        if not is_safe_http_url(cand.get('url')):
            continue
        try:
            download_file(cand['url'], part_path, retries=2)
            if not verify_duration(part_path, expected_interval):
                safe_unlink(part_path)
                raise RuntimeError(f'时长校验失败(预期{expected_interval})')
            if is_rejected_version(part_path):
                safe_unlink(part_path)
                raise RuntimeError('版本不符(伴奏/翻唱/卡拉ok等)')
            if not artist_matches_downloaded(part_path, expected_singer):
                safe_unlink(part_path)
                raise RuntimeError('歌手不符(疑似翻唱版)')
            if not verify_quality(part_path, requested_quality):
                safe_unlink(part_path)
                raise RuntimeError(f'音质不达标(低于 {requested_quality})')
            final_path = _path_with_ext(dest_base, detect_audio_ext(part_path))
            part_path.rename(final_path)
            return cand.get('quality', ''), str(final_path)
        except InterruptedError:
            # 用户停止：立刻放弃剩余候选并清掉半成品，不要继续试下一条链接。
            safe_unlink(part_path)
            raise
        except Exception as e:
            errors.append(f"{cand.get('quality', '?')}: {e}")
    # 清理残留 .part
    safe_unlink(part_path)
    raise RuntimeError('; '.join(errors))


def lower_quality_options(quality, min_quality='320k'):
    """返回当前音质及其以下音质，按优先级从高到低排列；最低不降到 min_quality 以下。
    min_quality 可取任意档位（128k/192k/320k/flac/flac24bit），表示允许的最低档位。
    例：quality=flac, min_quality=192k → [flac, 320k, 192k]"""
    ordered = ['128k', '192k', '320k', 'flac', 'flac24bit']  # 低 -> 高
    try:
        start = ordered.index(quality)
    except ValueError:
        return [quality]
    levels = list(reversed(ordered[:start + 1]))
    if min_quality in ordered:
        idx = ordered.index(min_quality)
        allowed = ordered[idx:]  # 含该档及更高档
        levels = [q for q in levels if q in allowed]
    return levels or [quality]


# ============ 内容校验（音质 / 时长 / 版本 / 歌手） ============
# 所有校验都基于下载文件的“真实内容”而非扩展名/URL：
# 伪 flac（低码率 MP3 换壳）、伴奏版、翻唱版在这里被统一拦截。


# 音质档位 → 要求的最低码率（bps）；None 表示必须是真无损。
QUALITY_MIN_BITRATE = {
    '128k': 128000,
    '192k': 192000,
    '320k': 320000,
    'flac': None,
    'flac24bit': None,
}
# 码率判定容差：VBR/V0 编码的实际平均码率常略低于标称 320k，放宽 5% 避免误杀。
_BITRATE_TOLERANCE = 0.95
# mutagen 中代表真无损的解析类型（小写类名）。
_LOSSLESS_TYPE_NAMES = {
    'flac', 'wave', 'aiff', 'monkeysaudio', 'wavpack', 'tak', 'trueaudio',
    'dsf', 'dsdiff', 'als',
}


def quality_min_bitrate(quality):
    """返回该音质档位要求的最低码率；None 表示必须是真无损。
    未知档位按 320k 处理，避免拼写错误把门槛放低。"""
    return QUALITY_MIN_BITRATE.get(str(quality or '').lower(), 320000)


def is_lossless_audio(mf):
    """按 mutagen 解析结果判断是否为真无损（FLAC/ALAC/APE/WAV/AIFF/WavPack…）。"""
    if mf is None or getattr(mf, 'info', None) is None:
        return False
    from mutagen.flac import FLAC
    if isinstance(mf, FLAC):
        return True
    codec = str(getattr(mf.info, 'codec', '') or '').lower()
    if 'alac' in codec or 'flac' in codec or 'pcm' in codec:
        return True
    return type(mf).__name__.lower() in _LOSSLESS_TYPE_NAMES


def verify_quality(path, quality='320k'):
    """校验音频真实音质（按内容而非扩展名）。

    规则：
      - 请求 flac / flac24bit → 必须是真无损（伪 flac、低码率 MP3 一律拒绝）
      - 请求 128k / 192k / 320k → 码率不低于对应档位（含 5% 容差）；
        更高音质（含无损）同样通过，绝不因为“比要求的更好”而拒收
    无法解析的文件一律视为不达标。
    """
    try:
        from mutagen import File as MFile
        mf = MFile(path)
        if mf is None or mf.info is None:
            return False
        if is_lossless_audio(mf):
            return True
        min_br = quality_min_bitrate(quality)
        if min_br is None:
            return False
        br = int(getattr(mf.info, 'bitrate', 0) or 0)
        return br >= int(min_br * _BITRATE_TOLERANCE)
    except Exception:
        return False


def detect_audio_ext(path):
    """按文件内容魔数探测真实音频格式，返回规范扩展名。
    URL 推断的扩展名不可靠（代理链接常无扩展名，会把 FLAC 存成 .mp3），
    落盘前一律以内容为准。无法识别时回退 .mp3。"""
    try:
        with open(path, 'rb') as f:
            head = f.read(64)
    except Exception:
        return '.mp3'
    if head[:4] == b'fLaC':
        return '.flac'
    if head[:4] == b'OggS':
        return '.ogg'
    if head[:4] == b'RIFF' and head[8:12] == b'WAVE':
        return '.wav'
    if head[4:8] == b'ftyp':
        return '.m4a'
    if head[:3] == b'MAC':
        return '.ape'
    # ID3v2 标签或 MPEG 帧同步头 → MP3
    if head[:3] == b'ID3':
        return '.mp3'
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return '.mp3'
    return '.mp3'


def _path_with_ext(base_path, ext):
    """给不含扩展名的基础路径追加扩展名。
    不用 Path.with_suffix：歌名本身含点（如 "Mr.Chu"）时会被误当扩展名切掉。"""
    return base_path.parent / (base_path.name + ext)


def probe_duration(path):
    """解析音频文件时长（秒），支持 mp3/m4a/flac"""
    try:
        # 尝试 mutagen（若已安装）
        try:
            from mutagen import File as MFile
            mf = MFile(path)
            if mf is not None and mf.info and mf.info.length:
                return float(mf.info.length)
        except Exception:
            pass
        # 手动解析 MP3：只读文件头部（含 ID3 与首个音频帧头），避免对整文件占内存。
        # 时长按 CBR 估算：文件总大小 ×8 / 码率。
        file_size = os.path.getsize(path)
        with open(path, 'rb') as f:
            data = f.read(256 * 1024)
        if data[:3] == b'ID3':
            id3_size = ((data[6] & 0x7f) << 21) | ((data[7] & 0x7f) << 14) | ((data[8] & 0x7f) << 7) | (data[9] & 0x7f)
            pos = 10 + id3_size
        else:
            pos = 0
        while pos < len(data) - 4:
            if data[pos] == 0xFF and (data[pos + 1] & 0xE0) == 0xE0:
                break
            pos += 1
        if pos >= len(data) - 4:
            return None
        hdr = data[pos:pos + 4]
        ver = (hdr[1] >> 3) & 3
        bitrate_idx = (hdr[2] >> 4) & 0xF
        srate_idx = (hdr[2] >> 2) & 3
        rates_v1 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
        rates_v2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
        srates = [44100, 48000, 32000, 0]
        if bitrate_idx == 15 or srate_idx == 3:
            return None
        br = rates_v1[bitrate_idx] if ver == 3 else rates_v2[bitrate_idx]
        sr = srates[srate_idx]
        if br == 0 or sr == 0:
            return None
        duration = file_size * 8 / (br * 1000)
        return duration if duration > 0 else None
    except Exception:
        return None


def interval_to_seconds(interval):
    """将 '02:56' 或 '03:45.5' 格式转为秒数"""
    if not interval:
        return None
    try:
        parts = str(interval).strip().split(':')
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except Exception:
        pass
    return None


def verify_duration(path, expected_interval, tolerance=15):
    """校验下载文件时长与预期是否一致（秒），防止下错歌"""
    if not expected_interval:
        return True
    expected = interval_to_seconds(expected_interval)
    actual = probe_duration(path)
    if expected is None or actual is None:
        return True  # 无法校验时放行
    return abs(actual - expected) <= tolerance


# 伴奏/乐器/翻唱等“非原唱版本”关键词。某些接口会返回同曲的伴奏、卡拉OK或
# 他人翻唱版，标题/标签里带这些词。时长校验无法区分（伴奏常与原版等长），
# 需按标题标签硬拦截。
VERSION_REJECT_KEYWORDS = [
    'instrument', 'inst\\.', '伴奏', 'カラオケ', 'karaoke', 'off vocal', 'offvocal',
    'off\\s*vocal', '(?<![a-z])acoustic', '卡拉ok', '翻唱', 'cover\\.?$', '纯音乐',
    '(?i)remix', '(?i)sped[ _-]?up', '(?i)slowed', '(?i)live[ _-]version',
]
_VERSION_REJECT_RE = [re.compile(k, re.IGNORECASE) for k in VERSION_REJECT_KEYWORDS]


def is_rejected_version(path):
    """检查文件标签标题是否命中“非原唱版本”关键词（伴奏/翻唱/卡拉ok等）。
    命中返回 True（应拒绝）。无标签或无法解析返回 False（不误杀）。"""
    vers = []
    try:
        from mutagen import File as MFile
        mf = MFile(path)
        if mf is not None:
            tags = getattr(mf, 'tags', None) or {}
            for k, v in tags.items():
                if isinstance(v, (list, tuple)):
                    vers.extend(str(x) for x in v)
                elif isinstance(v, str):
                    vers.append(v)
            # 依类型取常见标题键
            if isinstance(tags, dict):
                for k in ('title', 'TIT2', '\xa9nam', '©nam'):
                    if k in tags:
                        v = tags[k]
                        if isinstance(v, (list, tuple)):
                            vers.extend(str(x) for x in v)
                        else:
                            vers.append(str(v))
    except Exception:
        return False
    for v in vers:
        for rx in _VERSION_REJECT_RE:
            if rx.search(v):
                return True
    return False


def read_artist_tag(path):
    """读取音频文件 artist 标签（尽力而为，返回去重后的字符串列表）。"""
    try:
        from mutagen import File as MFile
        mf = MFile(path)
        if mf is None:
            return []
        tags = getattr(mf, 'tags', None) or {}
        artists = []
        # 遍历所有键更鲁棒（部分容器对非法键的 in 判断会抛异常）
        for k, v in tags.items():
            if not isinstance(v, (list, tuple, str)):
                continue
            key = str(k).lower()
            if 'art' in key and 'artist' in key:
                if isinstance(v, (list, tuple)):
                    artists.extend(str(x) for x in v)
                else:
                    artists.append(v)
        # 兼容 ID3 TPE1（以字节字符串描述）
        if not artists:
            for k in ('artist', 'TPE1', '\xa9ART'):
                try:
                    if k not in tags:
                        continue
                    v = tags[k]
                except Exception:
                    continue
                if isinstance(v, (list, tuple)):
                    artists.extend(str(x) for x in v)
                else:
                    artists.append(v)
        return [a.strip() for a in artists if a and a.strip()]
    except Exception:
        return []


def artist_matches_downloaded(path, expected_singer):
    """若文件带 artist 标签，校验是否与预期歌手**严格**匹配。

    严格化点：不再吃掉括号内容与标点，避免「某某（feat. 谁）」被判成「某某」。
    预期歌手为空或标签为空时仍不做判断（返回 True），避免误杀无标签文件。
    """
    if not expected_singer:
        return True
    tags = read_artist_tag(path)
    if not tags:
        return True  # 无标签不做歌手校验，避免误杀
    exp_n = strict_song_key(expected_singer)
    if not exp_n:
        return True
    for a in tags:
        if strict_song_key(a) == exp_n:
            return True
    # 多歌手：预期按分隔符拆段后，每段都要能在标签里找到完全相等的一项
    exp_parts = [strict_song_key(x) for x in re.split(r'[、,，;&/]', expected_singer) if x.strip()]
    tag_norms = [strict_song_key(a) for a in tags]
    exp_parts = [e for e in exp_parts if e]
    if exp_parts:
        return all(any(e == t for t in tag_norms) for e in exp_parts)
    return False


# ============ 设置持久化与命令行界面 ============


CONFIG_PATH = Path(os.environ.get('APPDATA', APP_DIR)) / 'lx-downloader-config.json'


def _looks_mojibake(text):
    """判断字符串是否像被错误编码过的乱码路径。

    典型来源：中文路径经 GBK→UTF-8 双向误解码后残留「钀介洩闊充箰」这类生僻字串。
    这类目录名不是用户有意创建的，出现即说明配置已被污染。
    """
    s = str(text or '')
    if not s:
        return False
    # 中文 Windows 上被误解码的 UTF-8 字节常落在「钀 洩 闊 充 箰 涓 嬭 浇」这类生僻字区间；
    # 用高密度生僻字 + 常见乱码字做判定，避免误伤正常中文目录。
    mojibake_chars = '钀洩闊箰涓嬭浇鏂囦欢澶归煶涔愪笅杞戒腑鏂囦贡鐮佺殑鍒楄〃'
    hits = sum(1 for ch in s if ch in mojibake_chars)
    return hits >= 2


def _dir_is_usable(path):
    """判断下载目录是否可用。

    判定顺序很重要：
      1. 空值 → 不可用
      2. 乱码路径 → 不可用。必须先于任何文件系统操作：乱码目录往往能被成功创建，
         一旦先 mkdir，就会把“脏配置”误判成“可用目录”，并真的建出一个乱码目录
      3. 已存在且可写 → 可用
      4. 不存在但能创建（父目录存在或可逐级创建）→ 可用（首次下载允许自动创建）
    """
    text = str(path or '').strip()
    if not text:
        return False
    if _looks_mojibake(text):
        return False
    try:
        p = Path(text)
        if p.exists():
            if not p.is_dir():
                return False
            probe = p / '.lx-write-probe'
            probe.write_bytes(b'')
            safe_unlink(probe)
            return True
        # 尚未创建：尝试创建并写探针（父目录可逐级创建时才成功，
        # 不存在的盘符 / 无权限路径会抛 OSError → 判为不可用）
        p.mkdir(parents=True, exist_ok=True)
        probe = p / '.lx-write-probe'
        probe.write_bytes(b'')
        safe_unlink(probe)
        return True
    except OSError:
        return False


def load_config():
    """读取命令行菜单保存的设置；损坏或不存在时使用默认值。
    读取时会自愈两类历史脏数据：乱码路径、指向已失效临时目录的音源目录。"""
    cpu_count = os.cpu_count() or 4
    defaults = {
        'db': DEFAULT_DB,
        'dir': DOWNLOAD_DIR,
        'quality': '320k',
        'fallback_quality': False,
        # 默认并发 8：实测 16 首对比（320k，同一音源池）
        #   4 并发 122.3s / 8 并发 108.8s / 16 并发 141.2s（失败也更多）
        # 16 路会打爆平台限流，反而更慢。
        # 注意：这只是**起始窗口**，run_download 会按实测成功率动态升降
        # （失败率高就降到 4，全成功且仍有积压就往上加），因为最优值随
        # 网络与音源状态漂移——40 首测试里 6 并发 192s、12 并发 204s，
        # 而 20 首测试里 12 并发反而更快，固定值没有普适解。
        'workers': max(4, min(8, (cpu_count or 4))),
        'node': '',
        'api': '',
        'api_dir': USER_SOURCES_DIR,
    }
    try:
        with CONFIG_PATH.open('r', encoding='utf-8') as f:
            saved = json.load(f)
        defaults.update({k: v for k, v in saved.items() if k in defaults})
    except (OSError, ValueError, TypeError):
        pass
    # 确保用户音源目录存在（空目录时由前置校验给出“请先导入音源”的清晰提示）。
    try:
        os.makedirs(USER_SOURCES_DIR, exist_ok=True)
    except OSError:
        pass
    # 单文件 EXE 的 _MEIPASS 是每次启动都会变化的临时目录，不能持久化使用；
    # 同理，若保存的 api_dir 已不存在（如临时目录残留），自动回退到用户音源目录。
    saved_api_dir = str(defaults.get('api_dir') or '')
    if not saved_api_dir or '_MEI' in saved_api_dir or not os.path.isdir(saved_api_dir):
        defaults['api_dir'] = USER_SOURCES_DIR
    defaults['fallback_quality'] = bool(defaults.get('fallback_quality', False))
    # 下载目录自愈：早期版本曾把 GBK 乱码路径或临时目录写进配置，会导致
    # 首次下载时凭空建出一个乱码目录。这里统一回落到默认下载目录并回写配置。
    if not _dir_is_usable(defaults.get('dir')):
        defaults['dir'] = DOWNLOAD_DIR
        try:
            save_config(defaults)
        except OSError:
            pass
    return defaults


def save_config(config):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open('w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def reset_config():
    """删除持久化设置并返回默认配置。"""
    try:
        CONFIG_PATH.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f'重置设置失败：{exc}')
        return load_config()
    print('设置已重置为默认值。')
    return load_config()


def settings_menu(config):
    """命令行设置页：路径、音质、并发和音源服务均可持久化。"""
    print('\n========== 设置 ==========', flush=True)
    print('直接回车保留当前值；输入 q 返回主菜单。')
    fields = [
        ('db', '落雪音乐数据库路径'), ('dir', '下载目录'),
        ('quality', '默认音质（1-5）'),
        ('fallback_quality', '下载失败自动降低音质（1=是，2=否）'),
        ('workers', '同时下载歌曲数（1-32）'), ('node', 'Node.js 路径（通常无需设置）'),
        ('api', '单个音源文件（留空使用音源目录）'), ('api_dir', '音源目录'),
    ]
    for key, label in fields:
        current = config[key]
        if key == 'quality':
            current_number = next((n for n, item in QUALITY_OPTIONS.items() if item[0] == current), '3')
            print('1=128k 标准  2=192k 较高  3=320k 高品质  4=flac 无损  5=flac24bit Hi-Res')
            answer = input(f'{label}\n[当前 {current_number}：{QUALITY_LABELS.get(current, current)}] > ').strip()
        elif key == 'fallback_quality':
            current_value = '1' if current else '2'
            answer = input(f'{label}\n[当前 {current_value}] > ').strip()
        else:
            answer = input(f'{label}\n[{current}] > ').strip()
        if answer.lower() == 'q':
            return config
        if not answer:
            continue
        if key == 'quality':
            if answer not in QUALITY_OPTIONS:
                print('音质无效，请输入 1-5，保留原设置。'); continue
            answer = QUALITY_OPTIONS[answer][0]
        if key == 'fallback_quality':
            if answer not in ('1', '2'):
                print('请输入 1 或 2，保留原设置。'); continue
            answer = answer == '1'
        if key == 'workers':
            try:
                answer = max(1, min(32, int(answer)))
            except ValueError:
                print('并发数无效，保留原设置。'); continue
        config[key] = answer
    if getattr(sys, 'frozen', False) and '_MEI' in str(config.get('api_dir', '')):
        config['api_dir'] = USER_SOURCES_DIR
    save_config(config)
    print(f'设置已保存：{CONFIG_PATH}')
    return config


def check_sources(api_file, api_dir):
    """音源前置校验：返回缺失说明（None 表示可用）。

    音源必须存在（单文件模式）或目录非空（目录模式），否则下载必然全部失败，
    提前给出“请先导入音源”的清晰指引，而不是让每首歌都报一遍错。
    """
    if api_file:
        return None if Path(api_file).is_file() else f'[错误] 音源文件不存在: {api_file}'
    d = Path(api_dir)
    if not d.is_dir():
        return f'[错误] 音源目录不存在: {api_dir}'
    if not any(f.suffix.lower() in ('.js', '.json') for f in d.iterdir()):
        return f'[错误] 音源目录为空: {api_dir}'
    return None


def main_menu():
    """无参数启动时显示的主菜单。"""
    config = load_config()
    print('\n╔══════════════════════════════╗')
    print('║        落雪音乐下载器        ║')
    print('╚══════════════════════════════╝')
    print('1. 下载歌曲')
    print('2. 设置')
    print('3. 重置设置')
    print('0. 退出')
    while True:
        choice = input('\n请选择 [1/2/3/0]: ').strip()
        if choice == '1':
            return config
        if choice == '2':
            config = settings_menu(config)
            print('\n1. 下载歌曲\n2. 设置\n3. 重置设置\n0. 退出')
        elif choice == '3':
            confirm = input('确定重置所有设置吗？输入 yes 确认：').strip().lower()
            if confirm == 'yes':
                config = reset_config()
            else:
                print('已取消重置。')
            print('\n1. 下载歌曲\n2. 设置\n3. 重置设置\n0. 退出')
        elif choice == '0':
            return None
        else:
            print('输入无效，请选择 1、2、3 或 0。')


def request_stop(signum=None, frame=None):
    """Ctrl+C handler: stop scheduling new work and let current tasks finish safely."""
    if not DOWNLOAD_STOP_EVENT.is_set():
        DOWNLOAD_STOP_EVENT.set()
        print('\n\n[停止] 收到 Ctrl+C，正在停止新任务；当前歌曲完成后退出……', flush=True)
    else:
        print('\n[停止] 已请求停止，请稍候……', flush=True)


# ============ 主流程编排 ============
# run()：解析参数 → 探测数据库/导入歌单 → 选歌单 → 启动线程本地音源服务池
#        → 并发执行 process_song（管线见文件头注释）→ 失败歌曲二次重试 → 汇总报告


def run():
    import signal
    signal.signal(signal.SIGINT, request_stop)
    config = load_config()
    if len(sys.argv) == 1:
        config = main_menu()
        if config is None:
            return
    parser = argparse.ArgumentParser(description='落雪音乐歌单一键下载器')
    parser.add_argument('--db', default=config['db'], help='落雪音乐数据库路径（或 .lxmc/.json 歌单导出文件）')
    parser.add_argument('--list', action='store_true', help='列出所有歌单')
    parser.add_argument('--playlist', '-p', help='要下载的歌单 ID（如 default、love）')
    parser.add_argument('--quality', '-q', default=config['quality'], choices=['128k', '192k', '320k', 'flac', 'flac24bit'],
                        help='音质代码（默认使用设置值；设置菜单使用 1-5）')
    parser.add_argument('--fallback-quality', action='store_true', default=config['fallback_quality'],
                        help='下载失败时自动逐级降低音质')
    parser.add_argument('--min-quality', default='320k', choices=['128k', '192k', '320k', 'flac', 'flac24bit'],
                        help='降级时允许的最低音质（默认 320k，不降到其下）')
    # --dir 默认留空（而不是直接取设置值），是为了区分「用户显式指定」与「用默认」：
    # --retry-failed 没带目录时，下载目录必须等于失败清单所在目录，
    # 否则读的是一份清单、写的又是另一份，成功的歌永远移不出去。
    parser.add_argument('--dir', '-d', default=None, help='下载目录（默认使用设置值）')
    parser.add_argument('--workers', '-w', type=int, default=config['workers'], help='同时下载歌曲数（默认使用设置值）')
    parser.add_argument('--node', default=config['node'] or None, help='Node.js 可执行文件路径（EXE 已内置）')
    parser.add_argument('--api', default=config['api'] or None, help='音源脚本路径（JS 文件或 user_api.json）')
    parser.add_argument('--api-dir', default=config['api_dir'], help=f'音源目录（默认 {USER_SOURCES_DIR}，可在 GUI「音源」页导入）')
    parser.add_argument('--exclude', default='', help='排除歌曲，使用逗号分隔的歌名或“歌手 - 歌名”')
    parser.add_argument('--max', type=int, default=0, help='最多下载数量（0=全部，用于测试）')
    parser.add_argument('--export-songs', action='store_true',
                        help='导出歌单歌曲清单为“歌手 - 歌名”（与 --playlist/-p 搭配；去重、按歌单原顺序）')
    parser.add_argument('--stop-file', default=None,
                        help='优雅停止信号文件（GUI 使用：该文件出现即收尾退出，不直接强杀进程）')
    parser.add_argument('--interactive', action='store_true',
                        help='结束前等待按键（仅用于双击运行的控制台 EXE；GUI 调用不要传，否则会污染 stdout）')
    parser.add_argument('--search', default=None,
                        help='单曲搜索：按关键词搜索歌曲，以 JSON 输出候选列表（不下载）')
    parser.add_argument('--search-limit', type=int, default=30, help='单曲搜索返回条数上限（默认 30）')
    parser.add_argument('--list-songs', action='store_true',
                        help='以 JSON 输出指定歌单的歌曲列表（与 --playlist 搭配）')
    parser.add_argument('--song-file', default=None,
                        help='从 JSON 文件读取待下载歌曲列表（GUI 单曲下载用；绕过数据库/歌单）')
    # 用 nargs='?'：`--retry-failed <目录>` 与 `--retry-failed`（不带值，用 --dir/默认目录）
    # 都支持。GUI 传的是显式目录；手动跑时省略即可复用 --dir。
    # 不带值时 const='' 而不是 None —— None 是「未启用该模式」的哨兵，两者必须能区分。
    parser.add_argument('--retry-failed', nargs='?', const='', default=None, metavar='目录',
                        help='只重试上次失败的歌：读取该目录下 failed.txt 并只下载其中歌曲（省略目录则用 --dir）')
    parser.add_argument('--count-failed', nargs='?', const='', default=None, metavar='目录',
                        help='以 JSON 输出该目录 failed.txt 里的失败歌曲数量（GUI 用于显示重试按钮）')
    parser.add_argument('--probe-sources', action='store_true',
                        help='音源体检：逐个音源实测能否取到可下载链接，并测响应速度（JSON 输出）')
    parser.add_argument('--probe-quality', default='320k',
                        help='体检时请求的音质（默认 320k，避免无损源缺失造成误判）')
    parser.add_argument('--probe-timeout-ms', type=int, default=8000,
                        help='单个音源单次探测的超时毫秒数（默认 8000）')
    parser.add_argument('--check-update', action='store_true',
                        help='检查 GitHub 上是否有新版本（JSON 输出）')
    parser.add_argument('--update-repo', default=None,
                        help='检查更新用的仓库（默认 MyGodKnow/lx-music-downloader）')
    args = parser.parse_args()

    # 解析下载目录：命令行显式给了就用它，否则用设置里的默认值。
    # 注意顺序——必须先确定 args.dir，失败清单模式要用它当兜底目录。
    if not str(args.dir or '').strip():
        args.dir = config['dir']

    # GUI 优雅停止：先挂上信号文件监听，再做后续耗时操作。
    if args.stop_file:
        watch_stop_file(args.stop_file)

    # ---- 失败清单模式：不碰数据库/歌单，先于一切探测处理 ----
    # （放在 --search 之前：两者都走「无歌单」路径，且重试最需要尽快拉起下载）
    if args.retry_failed is not None:
        return run_retry_failed(args)
    if args.count_failed is not None:
        return run_count_failed(args)
    # 音源体检：不需要数据库/歌单，尽早处理
    if getattr(args, 'probe_sources', False):
        return run_probe_sources(args)
    # 检查更新：同样不需要数据库
    if getattr(args, 'check_update', False):
        return run_check_update(args)

    # ---- 单曲搜索模式：不需要数据库，先于一切数据库探测处理 ----
    if args.search is not None:
        return run_search(args)

    # ---- 单曲下载模式：歌曲来自 --song-file，不需要数据库与歌单 ----
    if args.song_file:
        try:
            songs = load_song_file(args.song_file)
        except Exception as exc:
            print(f'[错误] 读取歌曲文件失败: {exc}')
            return 1
        if not songs:
            print('[错误] 歌曲文件里没有可下载的歌曲')
            return 1
        print(f'\n单曲下载：共 {len(songs)} 首歌曲')
        return run_download(args, songs, db_path=None, title=None)

    # 自动探测数据库（支持官方版/便携版/fork/扩展名差异）；也支持导入 .lxmc 导出歌单
    db_path = detect_database(args.db)
    if not db_path:
        print('[错误] 未找到落雪音乐数据库。')
        print('请确认落雪音乐桌面版已安装并运行过，或用 --db 指定路径/歌单导出文件。')
        print('已探测的常见位置：')
        for p in DB_PATH_CANDIDATES[:6]:
            print(f'  {p}')
        sys.exit(1)
    if is_lxmc_file(db_path):
        print(f'[导入] 使用落雪导出的歌单文件: {db_path}')
        try:
            playlists = load_lxmc_playlists(db_path)
        except Exception as e:
            print(f'[错误] 歌单文件解析失败: {e}')
            sys.exit(1)
    else:
        playlists = list_playlists(db_path)
    if args.list:
        print('落雪音乐中的歌单：')
        for p in playlists:
            print(f'  [{p["id"]}] {p["name"]} ({p["cnt"]} 首)')
        return

    # ---- 列歌模式：以 JSON 输出指定歌单的歌曲（GUI 歌单页展开用） ----
    if args.list_songs:
        if not args.playlist:
            print('[错误] --list-songs 需要搭配 --playlist', flush=True)
            sys.exit(1)
        songs = get_songs(db_path, args.playlist) if not is_lxmc_file(db_path) \
            else load_lxmc_songs(db_path, args.playlist)
        out = []
        for s in songs:
            # 关键：必须把 meta 一起输出。
            # 数据库里的 id 是带平台前缀的（如 wy_30070747），
            # 而音源需要的是 meta 里的纯数字 songId（30070747）。
            # 之前只输出 5 个字段，导致 GUI 单曲下载把带前缀的 id 传给音源，
            # 音源认不出来 → 「首页选歌单后无法下单曲」。
            meta_raw = s.get('meta') or '{}'
            try:
                meta = json.loads(meta_raw) if isinstance(meta_raw, str) else dict(meta_raw)
            except Exception:
                meta = {}
            out.append({
                'name': s.get('name', ''),
                'singer': s.get('singer', ''),
                'source': s.get('source', ''),
                'interval': s.get('interval', ''),
                'id': str(s.get('id', '')),
                # 完整 meta 原样带回，前端写入 --song-file 后引擎即可还原
                'meta': meta,
            })
        print(json.dumps(out, ensure_ascii=False), flush=True)
        return

    # 选择歌单
    if not args.playlist:
        print('选择要下载的歌单：')
        for i, p in enumerate(playlists):
            print(f'  {i + 1}. {p["name"]} ({p["cnt"]} 首) [{p["id"]}]')
        try:
            sel = input('请输入序号: ').strip()
            playlist_id = playlists[int(sel) - 1]['id']
        except (ValueError, IndexError):
            print('输入无效')
            sys.exit(1)
    else:
        playlist_id = args.playlist

    # 查找歌单
    pl = next((p for p in playlists if p['id'] == playlist_id), None)
    if not pl:
        print(f'[错误] 未找到歌单: {playlist_id}')
        print('可用歌单:')
        for p in playlists:
            print(f'  [{p["id"]}] {p["name"]} ({p["cnt"]} 首)')
        sys.exit(1)

    # 获取歌曲（数据库或导入文件）
    if is_lxmc_file(db_path):
        songs = load_lxmc_songs(db_path, playlist_id)
    else:
        songs = get_songs(db_path, playlist_id)

    # ---- 导出模式：直接输出“歌手 - 歌名”清单（去重、歌单原顺序），不进入下载流程 ----
    if args.export_songs:
        lines = []
        seen = set()
        for s in songs:
            name = str(s.get('name', '') or '').strip()
            if not name:
                continue
            singer = str(s.get('singer', '') or '').strip()
            # 严格去重：只合并「完全同名同歌手」的多平台记录，
            # 不把「晴天」与「晴天 (伴奏)」合并成一行
            key = (strict_song_key(singer), strict_song_key(name))
            if key in seen:
                continue
            seen.add(key)
            lines.append(f'{singer} - {name}' if singer else name)
        for line in lines:
            print(line, flush=True)
        return

    if DOWNLOAD_STOP_EVENT.is_set():
        print('[停止] 启动阶段收到停止请求，已退出。', flush=True)
        return

    excluded = {x.strip().casefold() for x in args.exclude.split(',') if x.strip()}
    if excluded:
        songs = [s for s in songs if s['name'].casefold() not in excluded and f"{s['singer']} - {s['name']}".casefold() not in excluded]
    if args.max > 0:
        songs = songs[:args.max]
    return run_download(args, songs, db_path=db_path, title=pl['name'])


def run_download(args, songs, db_path=None, title=None):
    """下载一批歌曲（歌单下载与单曲下载共用）。

    songs: 与 get_songs() 同构的歌曲列表
    db_path: 落雪数据库路径（用于查同名替代源）；单曲下载传 None
    title: 歌单名（仅用于日志展示）；单曲下载传 None
    """
    print(f'\n歌单「{title}」共 {len(songs)} 首歌曲' if title else f'\n共 {len(songs)} 首歌曲')
    fallback_text = f"开启（最低降到 {args.min_quality}）" if args.fallback_quality else '关闭'
    print(f'音质: {args.quality} ({QUALITY_LABELS.get(args.quality, "")})  下载目录: {args.dir}')
    print(f'失败后自动降低音质（最低 {args.min_quality}）: {fallback_text}')
    print(f'同时下载歌曲数: {args.workers}\n')

    # 启动 Node 音源服务
    node_exe = args.node or find_node()
    if not node_exe:
        print('[错误] 未找到 Node.js，请安装或用 --node 指定路径')
        sys.exit(1)
    # 音源前置检查：没有可用音源时直接给出清晰指引，而不是让每个歌曲都报错
    missing = check_sources(args.api, args.api_dir)
    if missing:
        print(missing)
        print('提示：请在 GUI「音源」页导入音源脚本（.js / .json，或落雪导出的 user_api.json），')
        print('      或用 --api / --api-dir 指定音源。导入后音源保存在 %APPDATA%\\lx-downloader-sources。')
        sys.exit(1)
    print(f'正在启动 {args.workers} 个并行音源服务...')
    if DOWNLOAD_STOP_EVENT.is_set():
        print('[停止] 启动阶段收到停止请求，已退出。', flush=True)
        return
    # 每个工作线程使用独立 JSON-RPC 管道，避免单个全局锁把 URL 请求串行化。
    server_local = threading.local()
    servers = []
    servers_lock = threading.Lock()

    def get_worker_server():
        server = getattr(server_local, 'server', None)
        if server is None:
            server = UrlServer(node_exe, args.api, args.api_dir).start()
            server_local.server = server
            with servers_lock:
                servers.append(server)
        return server

    print('音源服务并行池已就绪，开始下载。\n')

    download_dir = Path(args.dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    # 统计
    stats = {'success': 0, 'fail': 0, 'skip': 0}
    fail_list = []
    failed_songs = []
    # 本次已「解决」的歌（成功落盘或已存在被跳过）。结束时要按这些键把旧清单里的
    # 同名条目摘掉，否则重试成功后清单还在，用户会反复重下已经下好的歌。
    resolved_keys = set()
    resolved_lock = threading.Lock()
    print_lock = threading.Lock()

    def mark_resolved(song):
        with resolved_lock:
            resolved_keys.add(_failed_key(song))

    def process_song(idx, song, retry=False):
        _t0 = time.time()
        _url_cost = 0.0
        _dl_cost = 0.0
        try:
            if DOWNLOAD_STOP_EVENT.is_set():
                return 'stopped'
            # 优先：按 歌手-歌名 匹配任意扩展名的已存在文件，存在则跳过
            # （放在取音源服务之前：已下载的歌不该再拉起 Node 子进程）
            existing = song_exists_in_dir(download_dir, song['singer'], song['name'])
            if existing and existing.stat().st_size > 1024:
                with print_lock:
                    print(f'[跳过] {song["name"]} - {song["singer"]} (已存在)')
                return 'skip'

            errors = []
            server = get_worker_server()
            # 目标文件基础名（不含扩展名）：扩展名在下载后按文件内容确定。
            # 用 safe_join 兜底：歌名/歌手来自音源脚本，必须确保拼出的路径不越出下载目录。
            dest_base = safe_join(
                download_dir,
                sanitize_filename(f"{song['singer']} - {song['name']}"),
            )
            # 尝试原源 + 替代源
            # 清单里来源为空（老 v1 格式没有记录 source）时不把这首歌当候选：
            # 空 source 会让音源报「不支持的平台: 」，白白耗掉 3 次退避重试（约 30s/首），
            # 而且每个 worker 都这样卡一遍。直接走按名搜索，又快又准。
            candidates_queue = [song] if song.get('source') else []
            # 只有歌单下载（有数据库）才查同名替代源；单曲下载没有数据库上下文
            if db_path:
                candidates_queue += get_alternative_sources(db_path, song)
            if retry:
                candidates_queue.reverse()
            tried_sources = set()

            # 单首歌的总时间预算：超过就放弃，避免一首卡住整个 worker。
            # 实测失败歌曲常因「多源 × 多音质档 × 每源 20s 超时」累积到 200-400s，
            # 而这段时间足以让其它 5-8 首歌完成下载。宁可把这首歌记进 failed.txt，
            # 也不要它独占一个并发位。
            song_deadline = _t0 + SONG_TIME_BUDGET
            budget_hit = False

            for cand in candidates_queue:
                if DOWNLOAD_STOP_EVENT.is_set():
                    return 'stopped'
                if time.time() > song_deadline:
                    budget_hit = True
                    errors.append('单曲时间预算耗尽')
                    break
                if cand['source'] in tried_sources:
                    continue
                tried_sources.add(cand['source'])
                try:
                    music_info = get_music_info(cand)
                    source = cand['source']
                    # 开启降级时，获取链接、下载和严格时长校验都失败后才切换低音质。
                    quality_levels = lower_quality_options(args.quality, args.min_quality if hasattr(args, 'min_quality') else '320k') if args.fallback_quality else [args.quality]
                    quality_errors = []
                    for requested_quality in quality_levels:
                        if time.time() > song_deadline:
                            budget_hit = True
                            quality_errors.append('单曲时间预算耗尽')
                            break
                        try:
                            _tu = time.time()
                            cands = server.get_urls(source, music_info, requested_quality)
                            _url_cost += time.time() - _tu
                            if not cands:
                                raise RuntimeError('音源未返回任何链接')
                            _td = time.time()
                            actual_q, _saved = download_with_candidates(
                                cands, dest_base, song.get('interval'), song.get('singer'),
                                requested_quality=requested_quality)
                            _dl_cost += time.time() - _td
                            with print_lock:
                                downgrade_note = f'，自动降级至 {requested_quality}' if requested_quality != args.quality else ''
                                print(f'[成功] {song["name"]} - {song["singer"]} [{actual_q}]{downgrade_note}')
                                _print_timing(song, _url_cost, _dl_cost, time.time() - _t0)
                            return 'success'
                        except Exception as quality_error:
                            quality_errors.append(f'{requested_quality}: {quality_error}')
                    raise RuntimeError('; '.join(quality_errors) or '所有音质均失败')
                except Exception as e:
                    errors.append(f'{cand["source"]}: {e}')

            # 预算耗尽就不再走「按名搜索」（它本身还有 30s 预算，会继续拖）
            if budget_hit:
                raise RuntimeError('; '.join(errors) if errors else '耗时超限')

            # 按名搜索兜底（不限定原平台）。返回多个候选，逐个下载实测内容，
            # 通过时长+音质校验才算成功，避免"伪无损"链接（标 FLAC 实为 48k MP4）导致的误判。
            # 复用 download_with_candidates 的四重校验 + 按内容定扩展名逻辑。
            try:
                _tu = time.time()
                cands = server.search_by_name_list(song['name'], song['singer'], song.get('interval'))
                _url_cost += time.time() - _tu
                if not cands:
                    raise RuntimeError('按名搜索无候选')
                _td = time.time()
                actual_q, _saved = download_with_candidates(
                    cands, dest_base, song.get('interval'), song.get('singer'),
                    requested_quality=args.quality)
                _dl_cost += time.time() - _td
                with print_lock:
                    print(f'[成功] {song["name"]} - {song["singer"]} [{actual_q}] (按名搜索)')
                    _print_timing(song, _url_cost, _dl_cost, time.time() - _t0)
                return 'success'
            except Exception as e:
                errors.append(f'按名搜索: {e}')

            raise RuntimeError('; '.join(errors) if errors else '无可用来源')
        except InterruptedError:
            return 'stopped'
        except Exception as e:
            with print_lock:
                print(f'[失败] {song["name"]} - {song["singer"]}: {e}')
                _print_timing(song, _url_cost, _dl_cost, time.time() - _t0)
            return ('fail', song['name'], song['singer'], str(e))
    start_time = time.time()
    worker_count = max(1, min(32, args.workers))
    # 自适应并发：固定并发不可靠——实测同一批歌 8 并发 139s、12 并发 97s，
    # 而另一轮 8 并发反而优于 16 并发（最优值随网络与音源状态漂移）。
    # 这里用「分批调度」：每批完成后根据实测成功率与耗时调整下一批的并发度。
    # 池子按最大并发建好复用，实际同时在跑的任务数由调度器控制。
    max_workers = min(32, max(worker_count, 12))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        total = len(songs)
        stopped_early = False
        done = 0
        pending_index = 0
        in_flight = {}          # future -> song
        cur_window = worker_count   # 当前允许的在途任务数
        import collections
        recent = collections.deque(maxlen=30)  # 最近结果：True=成功
        batch_start = time.time()
        batch_done = 0

        def submit_until_full():
            nonlocal pending_index
            while pending_index < total and len(in_flight) < cur_window:
                if DOWNLOAD_STOP_EVENT.is_set():
                    return
                s = songs[pending_index]
                fut = pool.submit(process_song, pending_index, s)
                in_flight[fut] = s
                pending_index += 1

        submit_until_full()

        while in_flight:
            # 等任意一个完成
            done_set, _ = wait(list(in_flight.keys()), return_when=FIRST_COMPLETED)
            for fut in done_set:
                song_obj = in_flight.pop(fut, None)
                done += 1
                batch_done += 1
                try:
                    result = fut.result()
                except Exception as exc:
                    result = ('fail', (song_obj or {}).get('name', 'unknown'),
                              (song_obj or {}).get('singer', ''), str(exc))
                    with print_lock:
                        print(f'\n[线程异常] {(song_obj or {}).get("name", "unknown")}: {exc}')
                if result == 'stopped' and DOWNLOAD_STOP_EVENT.is_set():
                    for p in in_flight:
                        p.cancel()
                    in_flight.clear()
                    stopped_early = True
                    break
                if result == 'success':
                    stats['success'] += 1
                    recent.append(True)
                    if song_obj:
                        mark_resolved(song_obj)
                elif result == 'skip':
                    stats['skip'] += 1
                    recent.append(True)
                    # 已存在也算解决：这首不需要再出现在失败清单里
                    if song_obj:
                        mark_resolved(song_obj)
                elif result == 'stopped':
                    continue
                else:
                    stats['fail'] += 1
                    recent.append(False)
                    if len(result) >= 4:
                        failure = (result[1], result[2], result[3])
                        fail_list.append(failure)
                        failed_songs.append((song_obj, failure))
                    else:
                        failure = (str(result), '', 'unknown')
                        fail_list.append(failure)
                        failed_songs.append((song_obj, failure))

                elapsed = time.time() - start_time
                speed = done / elapsed if elapsed > 0 else 0
                active = len(in_flight)
                try:
                    _progress = {
                        'done': done,
                        'total': total,
                        'success': stats['success'],
                        'fail': stats['fail'],
                        'skip': stats['skip'],
                        'active': active,
                        'speed': round(speed, 1),
                    }
                    print('##PROGRESS## ' + json.dumps(_progress, ensure_ascii=False), flush=True)
                except Exception:
                    pass

            if stopped_early:
                break

            # 每完成 6 首评估一次：失败率高就降并发，全成功且仍有积压就小幅升
            if batch_done >= 6:
                fail_rate = 1.0 - (sum(recent) / len(recent) if recent else 1.0)
                old_window = cur_window
                if fail_rate > 0.35:
                    cur_window = max(4, cur_window - 3)
                elif fail_rate < 0.12 and cur_window < max_workers:
                    cur_window = min(max_workers, cur_window + 2)
                if cur_window != old_window:
                    with print_lock:
                        print(f'[调度] 并发窗口 {old_window} → {cur_window}'
                              f'（近 {len(recent)} 首失败率 {fail_rate*100:.0f}%）', flush=True)
                batch_done = 0

            submit_until_full()
    print()

    # 停止时等待在途任务收尾：ThreadPoolExecutor 退出时会 join 所有运行中的任务，
    # 这期间它们看到 STOP_EVENT 会立刻放弃剩余候选，把已下完的文件正常落盘。
    if stopped_early and DOWNLOAD_STOP_EVENT.is_set():
        print('[停止] 正在等待在途任务收尾（已下载的文件会保留）……', flush=True)

    for worker_server in servers:
        worker_server.close()
    servers.clear()

    if failed_songs and not DOWNLOAD_STOP_EVENT.is_set():
        retry_songs = [song for song, _failure in failed_songs]
        print(f'\n开始二次重试：{len(retry_songs)} 首失败歌曲，切换音源顺序。')
        retry_servers = []
        retry_local = threading.local()

        def get_retry_server():
            server = getattr(retry_local, 'server', None)
            if server is None:
                server = UrlServer(node_exe, args.api, args.api_dir).start()
                retry_local.server = server
                with servers_lock:
                    retry_servers.append(server)
            return server

        old_get_worker_server = get_worker_server
        get_worker_server = get_retry_server
        retry_failures = []
        retry_workers = max(1, min(4, worker_count))
        with ThreadPoolExecutor(max_workers=retry_workers) as retry_pool:
            retry_futures = {retry_pool.submit(process_song, i, song, True): song for i, song in enumerate(retry_songs)}
            for retry_future in as_completed(retry_futures):
                song = retry_futures[retry_future]
                try:
                    retry_result = retry_future.result()
                except Exception as exc:
                    retry_result = ('fail', song['name'], song['singer'], str(exc))
                if retry_result == 'success':
                    stats['fail'] -= 1
                    stats['success'] += 1
                    fail_list = [item for item in fail_list if not (item[0] == song['name'] and item[1] == song['singer'])]
                    mark_resolved(song)
                elif retry_result == 'skip':
                    stats['fail'] -= 1
                    stats['skip'] += 1
                    fail_list = [item for item in fail_list if not (item[0] == song['name'] and item[1] == song['singer'])]
                    mark_resolved(song)
                elif retry_result != 'stopped':
                    retry_failures.append(retry_result)
        for retry_server in retry_servers:
            retry_server.close()
        if retry_failures:
            fail_list = [(item[1], item[2], item[3]) for item in retry_failures if len(item) >= 4]
        else:
            fail_list = []
        print(f'二次重试完成：仍失败 {len(fail_list)} 首。')

    # 总结
    print('\n' + '=' * 50)
    if DOWNLOAD_STOP_EVENT.is_set():
        print('下载已停止，已完成的文件会保留，未完成文件不会写入正式文件。')
    else:
        print('下载完成！')
    print(f'成功 {stats["success"]} 首，失败 {stats["fail"]} 首，跳过 {stats["skip"]} 首')
    print(f'保存位置: {download_dir}')
    report_path = download_dir / FAILED_FILE_NAME
    if fail_list:
        print('\n失败的歌曲：')
        for name, singer, err in fail_list:
            print(f'  {singer} - {name}: {err}')

    # 统一维护失败清单（v2）：
    #   1. 先读回旧清单，保留历次失败记录（用户可能还没重试过）
    #   2. 把本次已解决的歌（成功落盘 / 已存在跳过）摘掉，清单不会越滚越大
    #   3. 本次仍失败的歌以最新错误原因写回，并补上 source/interval
    #      （重试时先问原平台，比纯按名搜索命中率高）
    if resolved_keys or fail_list:
        merged = {}
        for old in load_failed_songs(download_dir):
            merged[_failed_key(old)] = old
        for key in resolved_keys:
            merged.pop(key, None)
        # failed_songs 是 (song_obj, failure) 列表，用它还原 source/interval
        origin_by_key = {}
        for song_obj, _failure in failed_songs:
            if isinstance(song_obj, dict):
                origin_by_key[_failed_key(song_obj)] = song_obj
        for name, singer, err in fail_list:
            item = {
                'name': name,
                'singer': singer,
                'source': '',
                'interval': '',
                'error': err,
            }
            origin = origin_by_key.get((strict_song_key(name), strict_song_key(singer)))
            if origin:
                item['source'] = str(origin.get('source') or '')
                item['interval'] = str(origin.get('interval') or '')
            merged[_failed_key(item)] = item
        write_failed_file(download_dir, list(merged.values()))
        if fail_list:
            print(f'\n失败清单已写入: {report_path}（共 {len(merged)} 首）')
            print('提示：失败可能是音源临时不可用，稍后可在「下载歌曲」页点「重试上次失败的歌」。')
        elif merged:
            print(f'\n失败清单还剩 {len(merged)} 首历史记录（本次未涉及）。')
        else:
            print('\n失败清单已清空（之前的失败歌曲本次都已补齐）。')


def resolve_failed_dir(args):
    """确定失败清单所在目录。

    取值顺序：`--retry-failed` / `--count-failed` 显式给的目录 → `--dir` → 设置里的默认目录。
    最后一级兜底很关键：GUI 的「重试」按钮在用户没填下载目录时**不传值**，
    此时必须落到引擎自己的默认目录，才能读到「开始下载」留在那里的 failed.txt。
    """
    explicit = getattr(args, 'retry_failed', None)
    if explicit is None:
        explicit = getattr(args, 'count_failed', None)
    text = str(explicit or '').strip()
    if text:
        return Path(text)
    if str(getattr(args, 'dir', '') or '').strip():
        return Path(args.dir)
    return Path(load_config()['dir'])


def run_count_failed(args):
    """输出失败清单里可重试的歌曲数量（JSON），供 GUI 决定是否显示重试按钮。

    只输出一行 JSON：stdout 是 GUI 的解析通道，任何多余输出都会破坏解析。
    文件不存在时输出 0（而不是报错）——「没有失败清单」是正常状态。
    """
    directory = resolve_failed_dir(args)
    songs = load_failed_songs(directory)
    print(json.dumps({'count': len(songs), 'dir': str(directory)}, ensure_ascii=False), flush=True)
    return 0


# 音源体检用的探测样本：选几首不同平台、不同年代、命中率高的常见歌。
# 用固定样本而不是随机歌，是为了让多次体检结果可比（同一把尺子）。
_PROBE_SAMPLES = [
    ('kw', '晴天', '周杰伦', '186016'),
    ('kw', '夜曲', '周杰伦', '186021'),
    ('wy', '晴天', '周杰伦', '186016'),
]


def run_check_update(args):
    """检查 GitHub 上是否有新版本，输出一行 JSON。

    只输出 JSON（stdout 是 GUI 的解析通道）。网络失败时返回 ok:false 而不是报错退出——
    「检查更新失败」不应打断用户任何正常操作。
    """
    node_exe = args.node or find_node()
    if not node_exe:
        print(json.dumps({'ok': False, 'error': '未找到 Node.js'}, ensure_ascii=False), flush=True)
        return 1

    repo = getattr(args, 'update_repo', None) or 'MyGodKnow/lx-music-downloader'

    # 注意：这里**不能**复用 UrlServer —— 它启动时必须加载音源目录里的脚本，
    # 而「检查更新」根本不需要音源。之前误把引擎目录当音源目录传进去，
    # 结果 Node 把 lx_url_server.js 自己当音源加载并报错。
    # 因此直接起一个最小的 Node 子进程，只发 checkUpdate 一条指令。
    # 注意：必须用 RESOURCE_DIR 而不是 dirname(__file__)。
    # PyInstaller 打包后 __file__ 指向临时解压目录，与真实的资源目录不一致，
    # 用错会导致找不到 lx_url_server.js（表现为「未收到响应」且 stderr 全空）。
    script = URL_SERVER
    if not os.path.isfile(script):
        print(json.dumps({'ok': False, 'error': '找不到 lx_url_server.js'}, ensure_ascii=False), flush=True)
        return 1

    cmd = [node_exe]
    # 注意：这里**刻意不加 --permission**。
    # --permission 是给「加载第三方音源脚本」用的隔离；而检查更新不加载任何
    # 第三方脚本，只发一个 HTTPS 请求。加上它反而会让 Node 内部某些 API
    # （如读取临时目录）被拒绝，触发 uncaughtException 后进程直接退出，
    # 表现为「未收到响应」且 stderr 只有一行 Access to this API has been restricted。
    # 不开隔离不会降低安全性：这里执行的只有我们自己的 lx_url_server.js。
    #
    # --update-only：让 Node 完全跳过音源加载。否则音源目录为空时
    # failNoSources 会 exit(1)，导致没导入音源的用户永远查不了更新。
    cmd += [script, '--update-only']

    proc = None
    try:
        if os.environ.get('LX_DEBUG_UPDATE') == '1':
            print(f'[update] 命令: {cmd}', file=sys.stderr, flush=True)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', errors='replace', bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            env=build_sandbox_env(),
        )
        # init() 失败时 Node 会 exit(1)，此时 stdout 只有 {"ready":true} 或什么都没有，
        # 但 checkUpdate 的响应仍可能已写入 —— 所以先读完 stdout 再判断。
        out, err = proc.communicate(
            json.dumps({'id': 1, 'cmd': 'checkUpdate', 'repo': repo}) + '\n',
            timeout=30,
        )
        if os.environ.get('LX_DEBUG_UPDATE') == '1':
            print(f'[update] rc={proc.returncode} stdout={out[:300]!r}', file=sys.stderr, flush=True)
            print(f'[update] stderr={err[:600]!r}', file=sys.stderr, flush=True)
        payload = {'ok': False, 'error': '未收到响应'}
        for line in (out or '').splitlines():
            line = line.strip()
            if not line.startswith('{'):
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get('id') == 1:
                payload = obj
        payload.pop('id', None)
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        return 0 if payload.get('ok') else 1
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc)[:200]}, ensure_ascii=False), flush=True)
        return 1
    finally:
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass


def run_probe_sources(args):
    """音源体检：逐个音源实测能否取到可下载链接，并测响应速度。

    输出一行 JSON（stdout 是 GUI 的解析通道，必须保持纯净）：
      {"sources":[{"name","status","okCount","total","speedMs","error"}],"probedAt":...}
    status: ok（全部成功）/ flaky（部分成功）/ dead（一次都没成功）

    **进度日志一律写 stderr**：stdout 只留给最终 JSON。
    GUI 的 spawn_log_reader 会同时读 stdout/stderr 并按行推送，
    所以 stderr 上的进度也会出现在界面日志区，但不会破坏 JSON 解析。

    设计取舍：
      - 每个源用同一组探测样本，保证横向可比；
      - 样本故意跨平台（kw + wy），避免只测一个平台时把「只支持某平台」的源误判为死；
      - 全失败才算 dead，部分成功记为 flaky —— 音源抖动很常见，一次失败不足以判死。
    """
    node_exe = args.node or find_node()
    if not node_exe:
        print('[错误] 未找到 Node.js', file=sys.stderr)
        return 1
    missing = check_sources(args.api, args.api_dir)
    if missing:
        print(missing, file=sys.stderr)
        return 1

    probes = [
        {'source': src, 'quality': args.probe_quality or '320k',
         'musicInfo': {'name': name, 'singer': singer, 'source': src,
                       'songId': song_id, 'id': song_id}}
        for src, name, singer, song_id in _PROBE_SAMPLES
    ]

    def log(msg):
        print(msg, file=sys.stderr, flush=True)

    log(f'[体检] 开始：{len(probes)} 个探测样本（'
        + '、'.join(f'{s[0]}:{s[1]}' for s in _PROBE_SAMPLES) + '）')
    log(f'[体检] 每个音源逐个实测，预计需要 1-2 分钟…')

    server = UrlServer(node_exe, args.api, args.api_dir).start()
    try:
        # 边跑边把进度写 stderr：Node 侧每完成一个音源就发一行 probeProgress
        def on_progress(line):
            try:
                obj = json.loads(line)
            except Exception:
                return
            if obj.get('event') != 'probeProgress':
                return
            name = obj.get('name', '?')
            status = obj.get('status', '')
            ok_n = obj.get('okCount', 0)
            total = obj.get('total', 0)
            speed = obj.get('speedMs')
            mark = {'ok': '正常', 'flaky': '不稳定', 'dead': '失活'}.get(status, status)
            tail = f'  {speed}ms' if speed is not None else ''
            log(f'[体检] {mark:<6} {name}（{ok_n}/{total}）{tail}')

        resp = server.request('probeSources', probes=probes,
                              timeoutMs=getattr(args, 'probe_timeout_ms', 8000),
                              on_event=on_progress)
    finally:
        server.close()

    if not resp.get('ok'):
        print(json.dumps({'error': resp.get('error', '体检失败')}, ensure_ascii=False), flush=True)
        return 1

    sources = resp.get('sources', [])
    ok_n = sum(1 for s in sources if s.get('status') == 'ok')
    flaky_n = sum(1 for s in sources if s.get('status') == 'flaky')
    dead_n = sum(1 for s in sources if s.get('status') == 'dead')
    log(f'[体检] 完成：正常 {ok_n} 个，不稳定 {flaky_n} 个，失活 {dead_n} 个')

    result = {'sources': sources, 'probedAt': resp.get('probedAt') or int(time.time() * 1000)}

    # 自己把结果写进缓存文件（.probe.json）。
    # GUI 侧是流式跑体检的（为了实时显示日志），拿不到返回值，
    # 所以由引擎负责落盘，前端在收到 done 事件后读这个文件。
    try:
        cache_dir = args.api_dir or USER_SOURCES_DIR
        if cache_dir and os.path.isdir(cache_dir):
            cache_path = os.path.join(cache_dir, '.probe.json')
            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False)
            log(f'[体检] 结果已缓存: {cache_path}')
    except Exception as exc:
        # 缓存写失败不影响本次结果输出
        log(f'[体检] 缓存写入失败（不影响本次结果）: {exc}')

    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


def run_retry_failed(args):
    """只重试上次失败的歌：读 <目录>/failed.txt，只下载其中歌曲。

    与普通歌单下载的区别：
      1. 歌单来源是失败清单，不需要数据库/歌单（但数据库仍可用于查替代源）
      2. 已存在的歌由 process_song 里的 song_exists_in_dir 直接跳过
      3. 下载完成后 run_download 会把成功的歌从清单里摘掉
    """
    directory = resolve_failed_dir(args)
    items = load_failed_songs(directory)
    if not items:
        print(f'[提示] {directory / FAILED_FILE_NAME} 里没有待重试的歌曲，无需重试。')
        return 0
    songs = [failed_to_song(item) for item in items]
    # 关键：重试时下载目录**必须**与清单目录一致，否则「成功的歌从清单移除」会写错地方
    # （读 X 的清单、写 Y 的清单，成功的歌永远移不出去，清单只增不减）。
    # 因此即使显式传了不同的 --dir 也以清单目录为准，并明确告知用户。
    if str(getattr(args, 'dir', '') or '').strip() and \
            Path(args.dir).resolve() != directory.resolve():
        print(f'[提示] --dir 已按失败清单目录覆盖为: {directory}')
    args.dir = str(directory)
    print(f'\n重试失败歌曲：共 {len(songs)} 首（来自 {directory / FAILED_FILE_NAME}）')
    # 清单里没有平台 ID，但如果有数据库，仍可查「同名其他平台版本」当替代源
    db_path = None
    try:
        detected = detect_database(args.db)
        if detected and not is_lxmc_file(detected):
            db_path = detected
    except Exception:
        db_path = None
    return run_download(args, songs, db_path=db_path, title=None)


def run_search(args):
    """单曲搜索模式：按关键词搜索歌曲，以 JSON 输出候选列表（不下载）。
    输出为单行 JSON 数组，字段与 GUI 搜索结果表对应：
      [{name, singer, source, interval, id, via, album, qualitys}]
    出错时以 [错误] 前缀输出到 stderr 并返回非零码。
    """
    node_exe = args.node or find_node()
    if not node_exe:
        print('[错误] 未找到 Node.js，请安装或用 --node 指定路径')
        return 1
    missing = check_sources(args.api, args.api_dir)
    if missing:
        print(missing)
        print('提示：请在 GUI「音源」页导入音源脚本后再搜索。')
        return 1
    server = None
    try:
        server = UrlServer(node_exe, args.api, args.api_dir).start()
        songs = server.search_songs(args.search, max(1, args.search_limit))
        print(json.dumps(songs, ensure_ascii=False), flush=True)
        return 0
    except Exception as exc:
        print(f'[错误] 搜索失败: {exc}')
        return 1
    finally:
        if server is not None:
            server.close()


def load_song_file(path):
    """读取 GUI 传入的待下载歌曲 JSON（单曲下载用）。

    支持两种形态：
      - 单首：{"name": "...", "singer": "...", "source": "kw", ...}
      - 多首：{"songs": [ {...}, {...} ]} 或直接 [...]
    返回与 get_songs() 同构的歌曲列表（含 meta 字段）。
    """
    raw = Path(path).read_text(encoding='utf-8')
    data = json.loads(raw)
    if isinstance(data, dict) and 'songs' in data:
        items = data['songs']
    elif isinstance(data, dict):
        items = [data]
    elif isinstance(data, list):
        items = data
    else:
        raise RuntimeError('歌曲文件格式无法识别')
    songs = []
    for s in items:
        if not isinstance(s, dict):
            continue
        name = str(s.get('name', '') or '').strip()
        if not name:
            continue
        singer = str(s.get('singer', '') or '').strip()
        meta = {k: v for k, v in s.items()
                if k not in ('name', 'singer', 'source', 'interval', 'meta') and v is not None}
        if isinstance(s.get('meta'), dict):
            meta.update(s['meta'])

        # 兼容音源期望的 ID 字段名。
        #
        # 关键：数据库里的 id 形如 `wy_30070747`（带平台前缀），而音源要的是
        # 纯数字 `30070747`。若 meta 已带纯数字 songId（歌单路径），以 meta 为准；
        # 否则把前缀剥掉，避免把 `wy_30070747` 直接喂给音源导致取链失败。
        sid = s.get('id') or s.get('songId') or s.get('songmid')
        bare_sid = re.sub(r'^[A-Za-z]{2}_', '', str(sid)) if sid is not None else None
        src = str(s.get('source', '') or '')

        # meta 里已有的 songId 优先（那是数据库原始 ID，最准确）
        meta_song_id = meta.get('songId') or meta.get('songmid') or meta.get('id')
        if meta_song_id is not None:
            # 若 meta 里也是带前缀的，同样剥掉
            meta_song_id = re.sub(r'^[A-Za-z]{2}_', '', str(meta_song_id))
        effective_id = meta_song_id or bare_sid

        if effective_id is not None:
            meta.setdefault('id', effective_id)
            meta.setdefault('songId', effective_id)
            if src == 'tx':
                meta.setdefault('songmid', str(effective_id))
        songs.append({
            'id': str(sid or ''),
            'name': name,
            'singer': singer,
            'source': src,
            'interval': s.get('interval', '') or '',
            'meta': json.dumps(meta, ensure_ascii=False),
        })
    return songs


def _write_crash_log(exc):
    try:
        log_path = Path(APP_DIR) / 'lx-downloader-crash.log'
        with log_path.open('a', encoding='utf-8') as f:
            f.write('\n' + '=' * 72 + '\n')
            f.write(time.strftime('%Y-%m-%d %H:%M:%S') + '\n')
            traceback.print_exc(file=f)
        return log_path
    except Exception:
        return None


if __name__ == '__main__':
    try:
        run()
    except KeyboardInterrupt:
        request_stop()
        print('已停止。')
    except Exception as exc:
        crash_log = _write_crash_log(exc)
        print(f'\n[致命错误] {exc}')
        if crash_log:
            print(f'详细错误已保存：{crash_log}')
    finally:
        # 双击控制台 EXE 时不要在完成/异常后直接关闭窗口。
        #
        # 关键：GUI（Tauri）调用时 stdin 被重定向到 NUL 设备，某些环境下
        # isatty() 仍会返回 True，于是这里会等待输入并打印提示语，
        # 污染 stdout —— 上层按 JSON 解析时就会报
        # 「返回了非 JSON 内容: 程序已结束，输入 q 退出：」。
        #
        # 因此追加三个条件：
        #   1. 只有显式传入 --interactive 才等待（GUI 永远不传）
        #   2. stdin 与 stdout 都必须是真终端
        #   3. 任何情况下都不把提示写到 stdout
        interactive = (
            getattr(sys, 'frozen', False)
            and '--interactive' in sys.argv
            and sys.stdin is not None
            and sys.stdout is not None
            and sys.stdin.isatty()
            and sys.stdout.isatty()
        )
        if interactive:
            try:
                while True:
                    answer = input('\n程序已结束，输入 q 退出：').strip().lower()
                    if answer == 'q':
                        break
                    print('请输入 q 退出程序。')
            except (EOFError, KeyboardInterrupt):
                pass
