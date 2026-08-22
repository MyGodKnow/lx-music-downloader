#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音源探针 v2：对每个音源文件单独跑 searchByName 全流程（搜索→真实ID→取链接→验证）。
这是最公平的测法：走的就是真实下载兜底路径，不误杀依赖平台 ID 的音源。"""
import json, subprocess, time, os, sys, threading

HERE = os.path.dirname(os.path.abspath(__file__))
URL_SERVER = os.path.join(HERE, 'lx_url_server.js')
SRC_DIR = os.path.join(os.environ.get('APPDATA', HERE), 'lx-downloader-sources')


def find_node():
    """定位 Node.js：优先系统 PATH，再试常见安装位置。"""
    import shutil
    exe = shutil.which('node')
    if exe:
        return exe
    for cand in [
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'nodejs', 'node.exe'),
        os.path.join(os.environ.get('ProgramFiles', ''), 'nodejs', 'node.exe'),
    ]:
        if cand and os.path.isfile(cand):
            return cand
    return 'node'


def _readline_thread(pipe, out):
    try:
        out[0] = pipe.readline()
    except Exception:
        out[0] = ''


def probe_file(fname, song, singer, interval):
    fpath = os.path.join(SRC_DIR, fname)
    result = {'file': fname, 'ok': False, 'detail': '', 'secs': 0.0}
    t0 = time.time()
    try:
        proc = subprocess.Popen(
            [find_node(), URL_SERVER, '--api', fpath],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding='utf-8', bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as e:
        result['detail'] = f'启动失败: {e}'
        result['secs'] = round(time.time() - t0, 1)
        return result

    # ready 行
    out = [None]
    th = threading.Thread(target=_readline_thread, args=(proc.stdout, out), daemon=True)
    th.start()
    th.join(12)
    if th.is_alive() or '"ready"' not in (out[0] or ''):
        try: proc.kill()
        except Exception: pass
        result['detail'] = '加载失败/未就绪'
        result['secs'] = round(time.time() - t0, 1)
        return result

    # searchByName 全流程（服务端内部 deadline 30s，给足 50s）
    out2 = [None]
    def ask():
        try:
            proc.stdin.write(json.dumps({'id': 1, 'cmd': 'searchByName',
                                         'name': song, 'singer': singer, 'interval': interval},
                                        ensure_ascii=False) + '\n')
            proc.stdin.flush()
            out2[0] = proc.stdout.readline()
        except Exception:
            out2[0] = ''
    t = threading.Thread(target=ask, daemon=True)
    t.start()
    t.join(50)
    try:
        proc.stdin.close(); proc.terminate(); proc.wait(timeout=3)
    except Exception:
        try: proc.kill()
        except Exception: pass

    result['secs'] = round(time.time() - t0, 1)
    if t.is_alive() or not out2[0]:
        result['detail'] = '全流程超时(50s)'
        return result
    try:
        resp = json.loads(out2[0])
    except Exception:
        result['detail'] = '响应解析失败'
        return result
    cands = resp.get('candidates') or []
    if resp.get('ok') and cands:
        result['ok'] = True
        result['detail'] = f"OK {len(cands)}候选 via {cands[0].get('via','?')[:24]}"
    else:
        result['detail'] = str(resp.get('error', '无结果'))[:60]
    return result


def main():
    files = sorted(f for f in os.listdir(SRC_DIR) if f.lower().endswith(('.js', '.json')))
    print(f'共 {len(files)} 个音源，逐个全流程实测（searchByName 晴天-周杰伦）...\n')
    results = []
    for i, f in enumerate(files, 1):
        r = probe_file(f, '晴天', '周杰伦', 269)
        results.append(r)
        mark = '有效' if r['ok'] else '死源'
        print(f"[{i:2d}/{len(files)}] {mark} | {r['secs']:5.1f}s | {r['file']} | {r['detail']}")
        sys.stdout.flush()
    dead = [r['file'] for r in results if not r['ok']]
    alive = [r['file'] for r in results if r['ok']]
    print(f'\n=== 有效 {len(alive)} ===')
    for f in alive: print('  ' + f)
    print(f'\n=== 死源 {len(dead)} ===')
    for f in dead: print('  ' + f)
    with open(os.path.join(HERE, '_probe_sources_v2_report.json'), 'w', encoding='utf-8') as fp:
        json.dump(results, fp, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()