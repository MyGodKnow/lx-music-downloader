#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lx_music_downloader.py - 落雪音乐歌单一键下载器

功能：
  1. 读取落雪音乐本地数据库（lx.data.db）或导出歌单（.lxmc/.json）
  2. 通过 Node 桥接服务（lx_url_server.js）+ 音源脚本实时获取播放链接
  3. 并发下载所有歌曲到指定目录，每首都过四重校验
  4. 可选：把 FLAC 等格式并行转换为 320k MP3，集中到 mp3/ 子目录

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
  python lx_music_downloader.py --convert --dir D:/Music   # 目录内音频转 MP3
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import queue
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

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
DOWNLOAD_STOP_EVENT = threading.Event()


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


# ============ 格式转换（FLAC/其他 → MP3） ============
BUNDLED_FFMPEG = os.path.join(RESOURCE_DIR, 'ffmpeg', 'ffmpeg.exe')
CONVERT_SUFFIXES = ('.flac', '.m4a', '.aac', '.ogg', '.wav', '.ape', '.mp3')


def find_ffmpeg():
    """查找 ffmpeg 可执行文件：内置 → 常见安装路径 → PATH"""
    candidates = [
        BUNDLED_FFMPEG,
        r'C:\Program Files\格式工厂\ffmpeg.exe',
        r'C:\Program Files\Red Giant\Trapcode Suite\Tools\ffmpeg.exe',
        r'C:\ffmpeg\bin\ffmpeg.exe',
        'ffmpeg',  # PATH
    ]
    for c in candidates:
        try:
            r = subprocess.run([c, '-version'], capture_output=True, timeout=10)
            if r.returncode == 0:
                return c
        except Exception:
            continue
    return None


def convert_one_to_mp3(src_path, ffmpeg, bitrate='320k', stop_event=None):
    """把单个音频文件转成 MP3（320k 起，兼容 ffmpeg）。
    返回 (成功, 目标mp3路径, 错误信息)。转换不删除原文件。"""
    src = Path(src_path)
    if src.suffix.lower() == '.mp3':
        return True, str(src), None  # 已是 MP3，跳过
    dest = src.with_suffix('.mp3')
    cmd = [ffmpeg, '-y', '-i', str(src), '-codec:a', 'libmp3lame', '-b:a', bitrate, '-map_metadata', '0', str(dest)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=600)
        if r.returncode == 0 and dest.exists() and dest.stat().st_size > 1024:
            return True, str(dest), None
        err = (r.stderr or b'').decode('utf-8', 'ignore')[-300:]
        return False, str(dest), err or f'ffmpeg 返回码 {r.returncode}'
    except Exception as e:
        return False, str(dest), str(e)


def convert_folder_to_mp3(folder, bitrate='320k', keep_original=True, stop_event=None, workers=None, move_mp3=False):
    """并行转换文件夹内所有 FLAC/其他格式为 MP3（每个 ffmpeg 独立子进程，线程并行安全）。
    keep_original=False 时转换成功后删除原文件；默认保留。
    move_mp3=True 时转换完成后把所有 .mp3 文件（含已存在的）移入 <folder>/mp3 子目录。
    workers 为 None 时按 CPU 核数-1 自动确定（至少 2，最多 16）。
    返回统计 {ok, fail, skip, converted: [路径], moved: [路径]}。"""
    if workers is None:
        workers = max(2, min(16, (os.cpu_count() or 4) - 1))
    from concurrent.futures import ThreadPoolExecutor, as_completed
    folder = Path(folder)
    if not folder.is_dir():
        raise RuntimeError(f'目录不存在: {folder}')
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError('未找到 ffmpeg，无法转换。请安装 ffmpeg 或放到常见路径。')
    stats = {'ok': 0, 'fail': 0, 'skip': 0, 'converted': [], 'failed': [], 'moved': []}
    files = sorted([f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in CONVERT_SUFFIXES])
    # 伪 MP3 修正：扩展名 .mp3 但内容是其他格式（如 FLAC 换壳，多为无扩展名代理链接
    # 误存所致）→ 改回真实扩展名，交给下方正常流程重新生成真 320k MP3。
    for f in files:
        if f.suffix.lower() == '.mp3' and detect_audio_ext(f) != '.mp3':
            real = detect_audio_ext(f)
            target = f.parent / (f.stem + real)
            if target.exists():
                continue  # 已有同名的真实格式文件，不动
            try:
                f.rename(target)
                print(f'  [修正] {f.name} 实为 {real[1:].upper()} 内容，已改回真实扩展名', flush=True)
            except Exception:
                pass
    files = sorted([f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in CONVERT_SUFFIXES])
    todo = [f for f in files if f.suffix.lower() != '.mp3']

    if stop_event is not None and stop_event.is_set():
        print('[停止] 转换未开始。', flush=True)
        return stats

    print_lock = threading.Lock()

    def _work(f):
        if stop_event is not None and stop_event.is_set():
            return f, 'stopped', None, None
        # 目标 MP3 已存在且大于 1KB 时直接跳过（避免重复转）
        dest = f.with_suffix('.mp3')
        if dest.exists() and dest.stat().st_size > 1024:
            return f, 'skip', dest, None
        ok, d, err = convert_one_to_mp3(f, ffmpeg, bitrate, stop_event)
        return f, ('ok' if ok else 'fail'), Path(d), err

    if workers > 1 and len(todo) > 1:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futs = [pool.submit(_work, f) for f in todo]
            _done = 0
            for fut in as_completed(futs):
                src, status, dest, err = fut.result()
                _done += 1
                if status == 'stopped':
                    print('[停止] 转换已中止。', flush=True)
                    break
                with print_lock:
                    if status == 'ok':
                        stats['ok'] += 1
                        stats['converted'].append(str(dest))
                        print(f'  [转换] {src.name} -> MP3 {bitrate}', flush=True)
                        if not keep_original:
                            try:
                                src.unlink()
                                print(f'  [删除原文件] {src.name}', flush=True)
                            except Exception:
                                print(f'  [警告] 原文件删除失败: {src.name}', flush=True)
                    elif status == 'skip':
                        stats['skip'] += 1
                    else:
                        stats['fail'] += 1
                        stats['failed'].append((str(src), err))
                        print(f'  [失败] {src.name}: {err}', flush=True)
                _emit_convert_progress(_done, len(todo), stats)
    else:
        _done = 0
        for f in todo:
            if stop_event is not None and stop_event.is_set():
                print('[停止] 转换已中止。', flush=True)
                break
            src, status, dest, err = _work(f)
            if status == 'ok':
                stats['ok'] += 1
                stats['converted'].append(str(dest))
                print(f'  [转换] {src.name} -> MP3 {bitrate}', flush=True)
                if not keep_original:
                    try:
                        src.unlink()
                        print(f'  [删除原文件] {src.name}', flush=True)
                    except Exception:
                        print(f'  [警告] 原文件删除失败: {src.name}', flush=True)
            elif status == 'skip':
                stats['skip'] += 1
            else:
                stats['fail'] += 1
                stats['failed'].append((str(src), err))
                print(f'  [失败] {src.name}: {err}', flush=True)
            _done += 1
            _emit_convert_progress(_done, len(todo), stats)

    # 转换完成后：把目录下所有 MP3 集中到 <folder>/mp3 子目录
    if move_mp3:
        stats['moved'] = move_mp3_to_subdir(folder)
    return stats


def _emit_convert_progress(done, total, stats):
    """输出结构化转换进度事件（前端识别 ##PROGRESS## 渲染进度条）。"""
    try:
        _p = {'done': done, 'total': total,
              'success': stats['ok'], 'fail': stats['fail'], 'skip': stats['skip']}
        print('##PROGRESS## ' + json.dumps(_p, ensure_ascii=False), flush=True)
    except Exception:
        pass


def move_mp3_to_subdir(folder):
    """把文件夹内所有 .mp3 文件移入 <folder>/mp3 子目录，返回移动数量。"""
    folder = Path(folder)
    target = folder / 'mp3'
    target.mkdir(parents=True, exist_ok=True)
    moved = []
    for f in sorted(folder.glob('*.mp3')):
        if not f.is_file():
            continue
        dest = target / f.name
        try:
            if dest.exists():
                dest.unlink()
            f.rename(dest)
            moved.append(str(dest))
        except Exception:
            try:
                import shutil
                shutil.move(str(f), str(dest))
                moved.append(str(dest))
            except Exception:
                pass
    return moved


# ============ Node 音源桥接客户端 ============
# 与 lx_url_server.js 的 JSON-RPC 管道封装：每个下载线程一个独立实例，
# 避免全局锁把取链接请求串行化（见 run() 里的 thread-local 池）。


class UrlServer:
    """与 Node 音源桥接服务通信（常驻子进程）"""

    def __init__(self, node_exe, api_file=None, api_dir=None, allow_untrusted=False):
        self.proc = None
        self.lock = threading.Lock()
        self.counter = 0
        self.node_exe = node_exe
        self.api_file = api_file
        self.api_dir = api_dir
        self.allow_untrusted = allow_untrusted

    def start(self):
        cmd = [self.node_exe, URL_SERVER]
        if self.api_file:
            cmd += ['--api', self.api_file]
        elif self.api_dir:
            cmd += ['--api-dir', self.api_dir]
        if self.allow_untrusted:
            cmd += ['--allow-untrusted']
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
        )
        # 等待 ready 行
        ready = self.proc.stdout.readline()
        if '"ready"' not in ready:
            err = self.proc.stderr.read() if self.proc.stderr else ''
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

    def request(self, cmd, **kwargs):
        with self.lock:
            self.counter += 1
            msg = {'id': self.counter, 'cmd': cmd, **kwargs}
            self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + '\n')
            self.proc.stdin.flush()
            resp = self.proc.stdout.readline()
            if not resp:
                raise RuntimeError('音源服务已退出')
            return json.loads(resp)

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

    def close(self):
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None


# ============ 文件名归一化与模糊匹配 ============


def sanitize_filename(name):
    """清理文件名非法字符"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', name)
    name = name.strip(' .')
    return name or 'untitled'


def norm_song_key(s):
    """归一化歌名/歌手用于模糊匹配：去括号内容、空白和标点，转小写。"""
    if not s:
        return ''
    s = re.sub(r'\([^)]*\)', '', str(s))   # 半角括号内容
    s = re.sub(r'（[^）]*）', '', str(s))  # 全角括号内容
    s = re.sub(r'\s+', '', s)
    s = re.sub(r'[!-/:-@\[-`{-~]', '', s)
    return s.lower()

def norm_song_key_match(a, b):
    """归一化后比较歌名是否匹配：全等 或 短名为长名前缀（版本后缀场景）。"""
    na = norm_song_key(a)
    nb = norm_song_key(b)
    if not na or not nb:
        return True
    if na == nb:
        return True
    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    return long.startswith(short)


# ============ 落雪数据库（只读访问） ============


def _connect_db_ro(db_path):
    """以只读方式打开落雪音乐数据库（只读绝不写入，避免生成 -wal/-shm 及损坏用户数据）。
    注意：落雪桌面版可能正在运行且把数据库置于 WAL 模式，只读打开不会创建 -wal 文件。"""
    return sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)


def get_alternative_sources(db_path, song, fuzzy=True):
    """查找同名歌曲的其他可用源（替代源回退）。
    默认做模糊匹配：歌名归一化（去掉 ()/（）/标点后）相同即视为同曲，可命中
    “萌妹进行曲” vs “萌妹进行曲（高阶萌妹成长指南）”这类带版本后缀的条目，
    从而拿到真正的无损/320k 版本。
    若来源是导入的 .lxmc/.json 歌单文件（非 SQLite），无数据库可查，返回空列表。"""
    if is_lxmc_file(str(db_path)):
        return []
    conn = _connect_db_ro(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    if fuzzy:
        cur.execute("""
            SELECT id, name, singer, source, interval, meta
            FROM my_list_music_info
            WHERE source != ? AND name != ?
            ORDER BY CASE source WHEN 'tx' THEN 0 WHEN 'wy' THEN 1 WHEN 'kw' THEN 2 WHEN 'kg' THEN 3 ELSE 4 END
        """, (song['source'], song['name']))
        cand = [dict(r) for r in cur.fetchall()]
        nk_name = norm_song_key(song['name'])
        nk_singer = norm_song_key(song['singer'])
        rows = []
        seen = set()
        for r in cand:
            # 版本后缀兼容：如“萌妹进行曲” 与 “萌妹进行曲（高阶萌妹成长指南）”视为同曲，
            # 从而把有 flac/320k 记录的 kw 版本找回来当替代源。
            if not norm_song_key_match(r['name'], song['name']):
                continue
            # 歌手完全匹配，或归一化后完全匹配（兼容多歌手顺序/别名）
            if nk_singer and norm_song_key(r['singer']) != nk_singer:
                continue
            if r['id'] in seen:
                continue
            seen.add(r['id'])
            rows.append(r)
    else:
        cur.execute("""
            SELECT id, name, singer, source, interval, meta
            FROM my_list_music_info
            WHERE name = ? AND singer = ? AND source != ?
            ORDER BY CASE source WHEN 'tx' THEN 0 WHEN 'wy' THEN 1 WHEN 'kw' THEN 2 WHEN 'kg' THEN 3 ELSE 4 END
        """, (song['name'], song['singer'], song['source']))
        rows = [dict(r) for r in cur.fetchall()]
    conn.close()
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
# 大文件（≥8MB）切 4 片并行下载再合并；服务器不支持 Range 时退化为
# 单线程流式下载。每次重试前清理残留 .part 分片，失败重试按指数退避。


def download_file(url, dest_path, timeout=120, retries=3, parts=4):
    """下载文件：优先多线程分片并行（大文件），不支持 Range 时退化为单线程流式；支持断点续传。"""
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    last_error = None
    for attempt in range(retries + 1):
        if DOWNLOAD_STOP_EVENT.is_set():
            raise InterruptedError('用户请求停止')
        try:
            headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'audio/*,*/*;q=0.8', 'Referer': url}
            total_size = _probe_content_length(url, headers, timeout)
            if total_size and total_size >= _MULTIPART_MIN_SIZE and parts > 1:
                _multipart_download(url, dest_path, total_size, parts, headers, timeout)
            else:
                _download_sequential(url, dest_path, timeout, headers)
            return True
        except Exception as e:
            last_error = e
            if DOWNLOAD_STOP_EVENT.is_set():
                raise InterruptedError('用户请求停止')
            # 清理可能残留的分片临时文件
            for i in range(parts):
                safe_unlink(dest_path.with_suffix(dest_path.suffix + f'.part{i}'))
            if attempt < retries:
                time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f'下载失败: {last_error}')


# 分片并行下载门槛：小于此大小的文件用单线程，避免小文件分片开销
_MULTIPART_MIN_SIZE = 8 * 1024 * 1024


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
                    raise InterruptedError('用户请求停止')
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
                total += len(chunk)
        if total < 1024:
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
    part_size = (total_size + parts - 1) // parts
    jobs = []
    for i in range(parts):
        start = i * part_size
        end = min(total_size - 1, start + part_size - 1)
        if start > end:
            break
        pp = dest_path.with_suffix(dest_path.suffix + f'.part{i}')
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
    with open(dest_path, 'wb') as out:
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
    """检查歌单中某首歌是否已下载（按 歌手-歌名 归一化模糊匹配任意扩展名）。
    可命中已下载的变体版本（如“歌名 (Live)”），避免重复下载；模糊命中后
    仍由下载流程按时长+内容校验把关，不会因误判而跳过必需的重新下载。"""
    base_name = sanitize_filename(f"{singer} - {name}")
    try:
        for f in download_dir.iterdir():
            if f.is_file() and f.suffix.lower() in ('.mp3', '.m4a', '.flac', '.aac', '.wav', '.ogg', '.ape'):
                if norm_song_key_match(f.stem, base_name):
                    return f
    except Exception:
        pass
    return None


def download_with_candidates(candidates, dest_base, expected_interval=None, expected_singer=None):
    """尝试多个候选 URL；每个候选都独立临时文件，避免坏链接污染下一个候选。
    可传 expected_singer 做歌手标签校验，防止翻唱版冒充原唱。
    dest_base 为不含扩展名的目标路径；四重校验通过后按文件内容确定真实扩展名
    再落盘（URL 扩展名不可靠，避免把 FLAC 存成假 .mp3）。
    返回 (音质标签, 实际保存路径)。"""
    errors = []
    part_path = Path(str(dest_base) + '.part')
    for cand in candidates:
        safe_unlink(part_path)
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
            if not verify_quality(part_path):
                safe_unlink(part_path)
                raise RuntimeError('音质不达标(非无损且<320k)')
            final_path = _path_with_ext(dest_base, detect_audio_ext(part_path))
            part_path.rename(final_path)
            return cand.get('quality', ''), str(final_path)
        except Exception as e:
            errors.append(f"{cand.get('quality', '?')}: {e}")
    # 清理残留 .part
    safe_unlink(part_path)
    raise RuntimeError('; '.join(errors))


def lower_quality_options(quality, min_quality='320k'):
    """返回当前音质及其以下音质，按优先级从高到低排列；最低不降到 min_quality 以下（默认 320k，
    绝不降级到 192k/128k）。min_quality 可取值 320k/flac/flac24bit，表示允许的最低档位。"""
    ordered = ['320k', 'flac', 'flac24bit']  # 低 -> 高
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


def verify_quality(path):
    """校验音频真实格式（按内容而非扩展名）：无损(FLAC/ALAC/APE/WAV)或码率>=320k 才通过。
    伪 flac（实际是低码率 MP3/MP4）或无法解析的一律视为不达标。"""
    try:
        from mutagen import File as MFile
        from mutagen.flac import FLAC
        mf = MFile(path)
        if mf is None or mf.info is None:
            return False
        if isinstance(mf, FLAC):
            return True
        codec = getattr(mf.info, 'codec', None)
        if codec and 'alac' in str(codec).lower():
            return True
        br = int(getattr(mf.info, 'bitrate', 0) or 0)
        return br >= 320000
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
    """若文件带 artist 标签，校验是否与预期歌手匹配（归一化后）。
    预期歌手为空或标签为空时不做判断（返回 True，不误杀）。"""
    if not expected_singer:
        return True
    tags = read_artist_tag(path)
    if not tags:
        return True  # 无标签不做歌手校验，避免误杀
    exp_n = norm_song_key(expected_singer)
    for a in tags:
        if norm_song_key(a) == exp_n:
            return True
    # 多歌手：任一标签与预期任一分段匹配
    exp_parts = [norm_song_key(x) for x in re.split(r'[、,，;&/]', expected_singer) if x.strip()]
    tag_norms = [norm_song_key(a) for a in tags]
    if exp_parts:
        return all(any(e == t for t in tag_norms) for e in exp_parts)
    return False


# ============ 设置持久化与命令行界面 ============


CONFIG_PATH = Path(os.environ.get('APPDATA', APP_DIR)) / 'lx-downloader-config.json'


def load_config():
    """读取命令行菜单保存的设置；损坏或不存在时使用默认值。"""
    cpu_count = os.cpu_count() or 4
    defaults = {
        'db': DEFAULT_DB,
        'dir': DOWNLOAD_DIR,
        'quality': '320k',
        'fallback_quality': False,
        'workers': max(4, min(12, cpu_count * 2)),  # 默认按 CPU 核数×2，至少 4
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


def convert_menu(config):
    """格式转换子菜单：并行转换，完成后自动把所有 MP3 集中到 mp3 子文件夹。"""
    print('\n========== 格式转换（→ MP3） ==========')
    print('把下载目录内 FLAC / M4A / OGG / WAV 等格式转换为 MP3 320kbps。')
    print('转换完成后自动把所有 MP3 文件移入 mp3 子文件夹。')
    folder = input(f'转换目录\n[当前 {config["dir"]}] > ').strip() or config['dir']
    if not Path(folder).is_dir():
        print(f'[错误] 目录不存在: {folder}')
        return config
    while True:
        print('1=保留原文件  2=转换后删除原文件  3=移入原文件子目录')
        answer = input('[当前 1] > ').strip() or '1'
        if answer in ('1', '2', '3'):
            break
        print('输入无效，请输入 1、2 或 3。')
    auto_workers = max(2, min(16, (os.cpu_count() or 4) - 1))
    workers_input = input(f'并行数（0=按CPU核数自动={auto_workers}，直接回车自动）\n[当前 0] > ').strip()
    workers = None
    if workers_input.isdigit():
        workers = max(1, min(16, int(workers_input))) if int(workers_input) > 0 else None
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print('[错误] 未找到 ffmpeg，无法转换。')
        print('请安装 ffmpeg 并加入 PATH，或放到以下位置之一：')
        print('  C:\\Program Files\\格式工厂\\ffmpeg.exe')
        print('  C:\\ffmpeg\\bin\\ffmpeg.exe')
        return config
    print(f'使用 ffmpeg: {ffmpeg}')
    target_count = len([f for f in Path(folder).iterdir() if f.is_file() and f.suffix.lower() in CONVERT_SUFFIXES and f.suffix.lower() != '.mp3'])
    existing_mp3 = len(list(Path(folder).glob('*.mp3')))
    print(f'待转换 {target_count} 个文件，已存在 {existing_mp3} 个 MP3，并行 {workers or auto_workers} 路。')
    stats = convert_folder_to_mp3(folder, '320k', keep_original=(answer == '1'), stop_event=DOWNLOAD_STOP_EVENT,
                                  workers=workers, move_mp3=True)
    print(f'\n转换完成：成功 {stats["ok"]}，失败 {stats["fail"]}，跳过 {stats["skip"]}。')
    print(f'共 {len(stats["moved"])} 个 MP3 已移入 {Path(folder) / "mp3"} 文件夹。')
    if stats['failed']:
        print('失败文件：')
        for name, err in stats['failed'][:10]:
            print(f'  {name}: {err}')
    if answer == '3':
        # 移入原文件子目录
        keep_dir = Path(folder) / 'mp3_原文件'
        keep_dir.mkdir(parents=True, exist_ok=True)
        srcs = sorted([f for f in Path(folder).iterdir()
                       if f.is_file() and f.suffix.lower() in CONVERT_SUFFIXES and f.suffix.lower() != '.mp3'])
        moved = 0
        for src in srcs:
            try:
                src.rename(keep_dir / src.name)
                moved += 1
            except Exception:
                pass
        print(f'已将 {moved} 个原文件移入 {keep_dir}')
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
    print('4. 格式转换（FLAC等 → MP3）')
    print('0. 退出')
    while True:
        choice = input('\n请选择 [1/2/3/4/0]: ').strip()
        if choice == '1':
            return config
        if choice == '2':
            config = settings_menu(config)
            print('\n1. 下载歌曲\n2. 设置\n3. 重置设置\n4. 格式转换（FLAC等 → MP3）\n0. 退出')
        elif choice == '3':
            confirm = input('确定重置所有设置吗？输入 yes 确认：').strip().lower()
            if confirm == 'yes':
                config = reset_config()
            else:
                print('已取消重置。')
            print('\n1. 下载歌曲\n2. 设置\n3. 重置设置\n4. 格式转换（FLAC等 → MP3）\n0. 退出')
        elif choice == '4':
            config = convert_menu(config)
            print('\n1. 下载歌曲\n2. 设置\n3. 重置设置\n4. 格式转换（FLAC等 → MP3）\n0. 退出')
        elif choice == '0':
            return None
        else:
            print('输入无效，请选择 1、2、3、4 或 0。')


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
    parser.add_argument('--dir', '-d', default=config['dir'], help='下载目录')
    parser.add_argument('--workers', '-w', type=int, default=config['workers'], help='同时下载歌曲数（默认使用设置值）')
    parser.add_argument('--node', default=config['node'] or None, help='Node.js 可执行文件路径（EXE 已内置）')
    parser.add_argument('--api', default=config['api'] or None, help='音源脚本路径（JS 文件或 user_api.json）')
    parser.add_argument('--api-dir', default=config['api_dir'], help=f'音源目录（默认 {USER_SOURCES_DIR}，可在 GUI「音源」页导入）')
    parser.add_argument('--allow-untrusted-sources', action='store_true',
                        help='兼容保留：当前版本导入的音源默认全部加载，无需该开关')
    parser.add_argument('--exclude', default='', help='排除歌曲，使用逗号分隔的歌名或“歌手 - 歌名”')
    parser.add_argument('--max', type=int, default=0, help='最多下载数量（0=全部，用于测试）')
    parser.add_argument('--convert', action='store_true', help='转换目录内音频为 MP3（配合 --dir 指定目录）')
    parser.add_argument('--convert-bitrate', default='320k', help='MP3 码率（默认 320k）')
    parser.add_argument('--convert-keep', choices=['keep', 'delete', 'move'], default='keep',
                        help='原文件处理：keep 保留 / delete 删除 / move 移入子目录（默认 keep）')
    parser.add_argument('--convert-workers', type=int, default=0, help='并行转换进程数（0=按CPU核数自动，默认）')
    parser.add_argument('--convert-move-mp3', action='store_true', default=True,
                        help='转换完成后把所有 MP3 移入 <目录>/mp3 子文件夹（默认开启）')
    args = parser.parse_args()

    # 纯转换模式：不进数据库流程
    if args.convert:
        convert_keep = {'keep': True, 'delete': False, 'move': 'move'}[args.convert_keep]
        convert_workers = args.convert_workers or None  # 0/未传 → 按 CPU 核数自动
        print(f'开始转换目录 {args.dir} → MP3（{args.convert_bitrate}，并行 {convert_workers or "自动"}）…')
        stats = convert_folder_to_mp3(args.dir, args.convert_bitrate,
                                      keep_original=(convert_keep is True), stop_event=DOWNLOAD_STOP_EVENT,
                                      workers=convert_workers, move_mp3=args.convert_move_mp3)
        print(f'\n转换完成：成功 {stats["ok"]}，失败 {stats["fail"]}，跳过 {stats["skip"]}。')
        if args.convert_move_mp3:
            print(f'已把 {len(stats["moved"])} 个 MP3 移入 {Path(args.dir) / "mp3"}')
        if stats['failed']:
            print('失败文件：')
            for name, err in stats['failed'][:10]:
                print(f'  {name}: {err}')
        if convert_keep == 'move':
            keep_dir = Path(args.dir) / 'mp3_原文件'
            keep_dir.mkdir(parents=True, exist_ok=True)
            moved = 0
            for src in [f for f in Path(args.dir).iterdir()
                        if f.is_file() and f.suffix.lower() in CONVERT_SUFFIXES and f.suffix.lower() != '.mp3']:
                try:
                    src.rename(keep_dir / src.name)
                    moved += 1
                except Exception:
                    pass
            print(f'已将 {moved} 个原文件移入 {keep_dir}')
        return

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
    excluded = {x.strip().casefold() for x in args.exclude.split(',') if x.strip()}
    if excluded:
        songs = [s for s in songs if s['name'].casefold() not in excluded and f"{s['singer']} - {s['name']}".casefold() not in excluded]
    if args.max > 0:
        songs = songs[:args.max]
    print(f'\n歌单「{pl["name"]}」共 {len(songs)} 首歌曲')
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
    # 每个工作线程使用独立 JSON-RPC 管道，避免单个全局锁把 URL 请求串行化。
    server_local = threading.local()
    servers = []
    servers_lock = threading.Lock()

    def get_worker_server():
        server = getattr(server_local, 'server', None)
        if server is None:
            server = UrlServer(node_exe, args.api, args.api_dir, getattr(args, 'allow_untrusted_sources', False)).start()
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
    print_lock = threading.Lock()

    def process_song(idx, song, retry=False):
        _t0 = time.time()
        _url_cost = 0.0
        _dl_cost = 0.0
        try:
            if DOWNLOAD_STOP_EVENT.is_set():
                return 'stopped'
            # 优先：按 歌手-歌名 匹配任意扩展名的已存在文件，存在则跳过
            existing = song_exists_in_dir(download_dir, song['singer'], song['name'])
            if existing and existing.stat().st_size > 1024:
                with print_lock:
                    print(f'[跳过] {song["name"]} - {song["singer"]} (已存在)')
                return 'skip'

            errors = []
            server = get_worker_server()
            # 目标文件基础名（不含扩展名）：扩展名在下载后按文件内容确定
            dest_base = download_dir / sanitize_filename(f"{song['singer']} - {song['name']}")
            # 尝试原源 + 替代源
            candidates_queue = [song]
            candidates_queue += get_alternative_sources(db_path, song)
            if retry:
                candidates_queue.reverse()
            tried_sources = set()

            for cand in candidates_queue:
                if DOWNLOAD_STOP_EVENT.is_set():
                    return 'stopped'
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
                        try:
                            _tu = time.time()
                            cands = server.get_urls(source, music_info, requested_quality)
                            _url_cost += time.time() - _tu
                            if not cands:
                                raise RuntimeError('音源未返回任何链接')
                            _td = time.time()
                            actual_q, _saved = download_with_candidates(cands, dest_base, song.get('interval'), song.get('singer'))
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
                actual_q, _saved = download_with_candidates(cands, dest_base, song.get('interval'), song.get('singer'))
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
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {pool.submit(process_song, i, s): s for i, s in enumerate(songs)}
        total = len(songs)
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                result = fut.result()
            except Exception as exc:
                result = ('fail', futures[fut].get('name', 'unknown'), futures[fut].get('singer', ''), str(exc))
                with print_lock:
                    print(f'\n[线程异常] {futures[fut].get("name", "unknown")}: {exc}')
            if result == 'stopped' and DOWNLOAD_STOP_EVENT.is_set():
                for pending in futures:
                    if not pending.done():
                        pending.cancel()
                break
            if result == 'success':
                stats['success'] += 1
            elif result == 'skip':
                stats['skip'] += 1
            elif result == 'stopped':
                continue
            else:
                stats['fail'] += 1
                if len(result) >= 4:
                    failure = (result[1], result[2], result[3])
                    fail_list.append(failure)
                    failed_songs.append((futures[fut], failure))
                else:
                    failure = (str(result), '', 'unknown')
                    fail_list.append(failure)
                    failed_songs.append((futures[fut], failure))
            done = i
            elapsed = time.time() - start_time
            speed = done / elapsed if elapsed > 0 else 0
            # 进行中（未完成）的任务数：并发实时活跃度，避免进度"冻结→突跳"的观感
            active = max(0, len(futures) - done)
            # 结构化进度事件（前端识别 ##PROGRESS## 前缀渲染进度条，不进入日志区）
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
                print(f'\r进度: {done}/{total} | 成功 {stats["success"]} | 失败 {stats["fail"]} | '
                      f'跳过 {stats["skip"]} | 进行中 {active} | 速率 {speed:.1f}首/秒', end='', flush=True)
    print()

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
                server = UrlServer(node_exe, args.api, args.api_dir, getattr(args, 'allow_untrusted_sources', False)).start()
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
                elif retry_result == 'skip':
                    stats['fail'] -= 1
                    stats['skip'] += 1
                    fail_list = [item for item in fail_list if not (item[0] == song['name'] and item[1] == song['singer'])]
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
    if fail_list:
        print('\n失败的歌曲：')
        report_path = download_dir / 'failed.txt'
        # 追加而非覆盖，保留历次失败记录；每批前写时间戳分隔。
        with open(report_path, 'a', encoding='utf-8') as f:
            f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M')} ---\n")
            for name, singer, err in fail_list:
                line = f'{singer} - {name}: {err}'
                print(f'  {line}')
                f.write(line + '\n')
        print(f'\n失败清单已追加到: {report_path}')
        print('提示：失败可能是音源临时不可用，稍后可重新运行本程序重试。')


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
        # 双击控制台 EXE 时不要在完成/异常后直接关闭窗口；命令行调用不阻塞。
        if getattr(sys, 'frozen', False) and sys.stdin and sys.stdin.isatty():
            try:
                while True:
                    answer = input('\n程序已结束，输入 q 退出：').strip().lower()
                    if answer == 'q':
                        break
                    print('请输入 q 退出程序。')
            except (EOFError, KeyboardInterrupt):
                pass
