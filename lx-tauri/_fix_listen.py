# -*- coding: utf-8 -*-
"""修 listen 异步注册的清理竞态。

原写法：
    let unH;
    (async () => { unH = await listen(...) })();
    return () => unH?.();          // cleanup 跑在 unH 赋值之前 → 从不注销

StrictMode 双挂载、或组件很快卸载时，监听器会残留并重复触发
（日志重复、进度重复 set）。改用 cancelled 标志。
"""
import io

path = 'src/App.tsx'
with io.open(path, encoding='utf-8', newline='') as f:
    lines = f.read().split('\n')

# --- download-log ---
i = next(i for i, l in enumerate(lines) if 'let unH: (() => void) | undefined;' in l)
# 确认下一段是 download-log
assert 'download-log' in lines[i + 2], repr(lines[i + 2])

new_dl = '''    // cancelled 标志：listen() 是异步的，若在它 resolve 之前就 cleanup，
    // unH 还没被赋值，原写法会让监听器永久残留（StrictMode 下会重复订阅）。
    let cancelled = false;
    let unH: (() => void) | undefined;
    (async () => {
      const off = await listen<LogLine>("download-log", (e) => handleLog(e.payload));
      if (cancelled) {
        off();   // 已经卸载了，立刻注销，不要留下悬挂监听
      } else {
        unH = off;
      }
    })();
    return () => {
      cancelled = true;
      unH?.();
    };'''.split('\n')
lines[i:i + 3] = new_dl   # 替换 let unH / (async... / 那一行

# --- progress ---
j = next(k for k, l in enumerate(lines) if 'let unH: (() => void) | undefined;' in l and 'progress' in lines[k + 2])
new_pg = '''    let cancelled = false;
    let unH: (() => void) | undefined;
    (async () => {
      const off = await listen<string>("progress", (e) => {
        try {
          const p = JSON.parse(e.payload) as Progress;
          setProgress(p);
          // 注意：进度到 100% 不代表进程已退出（还有二次重试、写失败清单）。
          // 这里只标记「下载完成」，真正的 running 由 done 事件解除。
        } catch {
          /* ignore badly-formed progress */
        }
      });
      if (cancelled) {
        off();
      } else {
        unH = off;
      }
    })();
    return () => {
      cancelled = true;
      unH?.();
    };'''.split('\n')
# 找到 progress effect 的 let unH 到 return 结束（4 行）
lines[j:j + 3] = new_pg[:3]
# 剩下的清理块在 j+3..j+6（return / unH?.(); / };）替换
# 因为 new_pg 已包含完整的清理，删掉旧的清理块
k = j + 3
while not lines[k].strip().startswith('return () =>'):
    k += 1
# 删除 return { unH?.(); };
del lines[k:k + 3]

with io.open(path, 'w', encoding='utf-8', newline='') as f:
    f.write('\n'.join(lines))

s = '\n'.join(lines)
print('cancelled 标志数量:', s.count('let cancelled = false;'))
print('off() 立即注销:', s.count('off();'))
