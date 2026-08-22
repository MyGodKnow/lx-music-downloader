#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""健康扫描：对歌单抽样歌曲，逐个音源实测能否返回有效播放链接，输出每音源命中数。"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lx_music_downloader import UrlServer, find_node, get_music_info, DEFAULT_DB, load_config

def get_songs(db_path, playlist_id, limit=6):
    conn = sqlite3.connect(db_path)
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
    seen = set()
    out = []
    for r in rows:
        if r['id'] in seen:
            continue
        seen.add(r['id'])
        out.append(r)
    # 尽量覆盖不同平台
    by_source = {}
    for r in out:
        by_source.setdefault(r['source'], []).append(r)
    sample = []
    for src, lst in by_source.items():
        sample.extend(lst[:3])
    return sample[:limit]

def main():
    config = load_config()
    db = config.get('db') or DEFAULT_DB
    songs = get_songs(db, 'love', limit=12)
    print(f'抽样 {len(songs)} 首歌曲:')
    for s in songs:
        print(f"  [{s['source']}] {s['singer']} - {s['name']}")
    node = find_node()
    api_dir = os.path.join(os.environ.get('APPDATA', os.path.dirname(os.path.abspath(__file__))), 'lx-downloader-sources')
    server = UrlServer(node, None, api_dir).start()
    payload = [{'name': s['name'], 'singer': s['singer'], 'source': s['source'],
                'musicInfo': get_music_info(s), 'quality': '320k'} for s in songs]
    resp = server.request('scanSources', songs=payload)
    server.close()
    if not resp.get('ok'):
        print('扫描失败:', resp)
        return
    per = resp.get('perSource', {})
    print('\n=== 每音源命中歌曲数 ===')
    for name, cnt in sorted(per.items(), key=lambda x: -x[1]):
        print(f'  {cnt:2d}  {name}')
    print('\n=== 每首歌可用的音源 ===')
    for item in resp.get('report', []):
        wins = item.get('wins', [])
        print(f"  [{item['singer']} - {item['name']}] {len(wins)} 源: {', '.join(wins) or '无'}")
    print('\n=== 首歌曲逐源明细 ===')
    if resp.get('report'):
        first = resp['report'][0]
        for d in first.get('detail', []):
            print(f"  {d.get('src','?')}: ok={d.get('ok')} {d.get('url') or d.get('raw') or d.get('err')}")

    # 统计死源：被尝试至少 4 次且从未产出有效链接
    stat = {}
    for item in resp.get('report', []):
        for d in item.get('detail', []):
            name = d.get('src', '?')
            s = stat.setdefault(name, {'tries': 0, 'fails': 0})
            s['tries'] += 1
            if not d.get('ok'):
                s['fails'] += 1
    print('\n=== 疑似死源（尝试>=3 次且全部失败） ===')
    dead = [n for n, s in stat.items() if s['tries'] >= 3 and s['fails'] == s['tries']]
    for n in sorted(dead):
        print(f'  {n}')
    print('\n=== 可用的音源（按命中排序） ===')
    for name, cnt in sorted(per.items(), key=lambda x: -x[1]):
        print(f'  {cnt:2d}  {name}')

if __name__ == '__main__':
    main()
