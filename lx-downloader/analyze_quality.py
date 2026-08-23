#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按内容(而非扩展名)探测音频真实格式与码率，统计达标(无损或>=320k)情况。
用法: python analyze_quality.py [--move]  # --move 把不达标文件移入 low_quality/ 子目录
"""
import os
import sys
import shutil
from mutagen import File as MFile
from mutagen.flac import FLAC

# 与 lx_music_downloader.py 开发模式的 DOWNLOAD_DIR 对齐（同为脚本目录下 downloads/），
# 避免维护工具扫错目录（冻结版引擎则输出到 ~/Music/落雪音乐下载）。
DL = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'downloads')
MIN_BR = 320000
DO_MOVE = '--move' in sys.argv

def sniff(path):
    mf = MFile(path)
    if mf is None or mf.info is None:
        return ('未知', None, 0)
    if isinstance(mf, FLAC):
        return ('无损FLAC', 'FLAC', None)
    codec = getattr(mf.info, 'codec', None)
    if codec and 'alac' in str(codec).lower():
        return ('无损ALAC', 'ALAC', None)
    fmt = type(mf).__name__.upper()
    br = int(getattr(mf.info, 'bitrate', 0) or 0)
    return ('有损', fmt, br)

files = [f for f in os.listdir(DL) if os.path.isfile(os.path.join(DL, f)) and f.lower().endswith(('.mp3', '.m4a', '.flac', '.aac', '.ogg', '.wav', '.ape'))]
rows = []
for f in files:
    p = os.path.join(DL, f)
    try:
        kind, fmt, br = sniff(p)
    except Exception as e:
        kind, fmt, br = ('错误', type(e).__name__, 0)
    rows.append((f, kind, fmt, br))

lossless = sum(1 for r in rows if r[1].startswith('无损'))
ok320 = sum(1 for r in rows if r[1] == '有损' and r[3] and r[3] >= MIN_BR)
bad = [r for r in rows if not (r[1].startswith('无损') or (r[1] == '有损' and r[3] and r[3] >= MIN_BR))]

print(f'总文件: {len(rows)} | 真无损: {lossless} | >=320k: {ok320} | 不达标: {len(bad)}')

if DO_MOVE and bad:
    dst = os.path.join(DL, 'low_quality')
    os.makedirs(dst, exist_ok=True)
    moved = 0
    for f, kind, fmt, br in bad:
        src = os.path.join(DL, f)
        target = os.path.join(dst, f)
        if os.path.exists(target):
            base, ext = os.path.splitext(f)
            target = os.path.join(dst, f'{base}_{moved}{ext}')
        try:
            shutil.move(src, target)
            moved += 1
        except Exception as e:
            print(f'  移动失败 {f}: {e}')
    print(f'已移入 low_quality/: {moved} 个')
else:
    print('\n=== 不达标文件(真实格式) ===')
    for f, kind, fmt, br in bad:
        kb = f'{br/1000:.0f}k' if br else '-'
        print(f'  [{kind} {fmt}] {kb}  {f}')
