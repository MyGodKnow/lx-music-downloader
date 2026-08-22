#!/usr/bin/env node
/**
 * lx_url_server.js - 落雪音乐音源桥接服务（常驻模式）
 *
 * 架构：
 *   本进程在 Node 中模拟 lx-music 音源运行环境，加载 sources/ 目录下的音源脚本，
 *   通过 stdin/stdout 的 JSON-RPC 协议为 Python 主程序（lx_music_downloader.py）服务。
 *
 * 协议（每行一个 JSON）：
 *   -> {"id":1, "cmd":"url", "source":"kw", "musicInfo":{...}, "quality":"320k"}
 *   <- {"id":1, "ok":true, "candidates":[{url,quality,via},...]}
 *   其他命令：searchByName（按名搜索兜底，返回多候选）、
 *            scanSources（调试：逐源健康检查）、searchList（调试：多平台候选枚举）、ping。
 *
 * 文件结构（自上而下）：
 *   1. 参数解析与音源脚本加载（--api 单文件 / --api-dir 目录）
 *   2. HTTP 基础设施（doRequest / checkUrl 链接验证）
 *   3. lx 环境模拟（buildLx）与音源初始化（init）
 *   4. 歌曲字段规范化与匹配工具（normKw / nameMatch / singerMatch / intervalMatch…）
 *   5. URL 码率特征识别（urlLooks128k / urlLooks320 / urlLooksLossless）
 *   6. 取链接核心：音源成功榜 + 平台并发限制 + callHandler 统一入口
 *      + getUrlFromSourceEntry（单源取链）+ getMusicUrls（多源聚合，拿够即停）
 *   7. 按名搜索兜底 searchByName（五种方式逐级尝试，多候选返回）
 *   8. JSON-RPC 主循环
 *
 * 性能约定：
 *   - 音源成功榜（sourceScore）让近期可用的源优先被尝试，死源自动沉底；
 *   - getMusicUrls 拿够 2 个候选即停，整体预算 12s，绝不卡死单首歌；
 *   - 同平台并发上限 PLATFORM_MAX_CONCURRENT=3，避免打爆平台 API 触发限流。
 */
'use strict';

// 兜底：个别音源脚本（如混淆过的独家音源）会抛未捕获的 Promise 拒绝或异常，
// 若不拦截会直接把整个 Node 进程带崩，导致全部下载中断。这里统一吞掉并记录。
process.on('unhandledRejection', (e) => {
  try { process.stderr.write('[lx-url-server] unhandledRejection: ' + String((e && e.message) || e).slice(0, 200) + '\n'); } catch (_) {}
});
process.on('uncaughtException', (e) => {
  try { process.stderr.write('[lx-url-server] uncaughtException: ' + String((e && e.message) || e).slice(0, 200) + '\n'); } catch (_) {}
});

const vm = require('vm');
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');
const crypto = require('crypto');
const http = require('http');
const https = require('https');
const { URL } = require('url');
const readline = require('readline');

// ========== 参数解析 ==========
const args = process.argv.slice(2);
let apiFile = null;
let apiDir = null;
let allowUntrusted = false;
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--api' || args[i] === '-a') apiFile = args[++i];
  if (args[i] === '--api-dir') apiDir = args[++i];
  if (args[i] === '--allow-untrusted' || args[i] === '-u') allowUntrusted = true;
}

// 默认音源：优先加载内置 sources/ 目录（只保留经探测可用的音源），
// 目录为空时再退回落雪桌面版导出的 user_api.json。
function findDefaultApi() {
  const userApi = path.join(process.env.APPDATA || '', 'lx-music-desktop', 'LxDatas', 'user_api.json');
  return fs.existsSync(userApi) ? userApi : null;
}
if (!apiFile && !apiDir) {
  const builtinDir = path.join(__dirname, 'sources');
  const hasBuiltin = fs.existsSync(builtinDir)
    && fs.readdirSync(builtinDir).some(name => /\.(js|json)$/i.test(name));
  if (hasBuiltin) apiDir = builtinDir;
  else apiFile = findDefaultApi();
}
if (!apiFile && !apiDir) {
  console.error('未找到音源脚本，请用 --api 或 --api-dir 指定');
  process.exit(1);
}
if (apiFile && !fs.existsSync(apiFile)) {
  console.error('音源脚本不存在: ' + apiFile);
  process.exit(1);
}

// ========== 音源信任白名单（哈希固定） ==========
// 内置 5 个音源经全流程实测验证，SHA-256 固定。loadScripts 加载时会逐文件校验：
// 文件哈希不在白名单内的音源默认拒绝加载，需显式传 --allow-untrusted 才放行，
// 防止畸形/被篡改/来路不明的第三方音源在 vm 中执行（vm 不是强安全边界）。
const ALLOWED_SOURCE_HASHES = new Set([
  '5c0fd239110e4835533d7f289ad895cae2ded3917af501db0650f08f592265ea', // K×H测试 v1.7.17
  '9ddcc32f55ba1981166f16bf55c03c43165b098932208a5fb4c9c64192650f0c', // lx-玉宁熙-Pro v1.2.2
  'd6c7cabaeb77c4231092b0d8f9f8b9bf9f0e0511215669631957aceb4bf34eb2', // 墨澜聚合音源 v2.2.0
  'b45e05b8c043f0e715f9856ea400707bdf131b9c05b5014e1de9d0821a9972a7', // 屿溪-终章 v0.0.0
  '48d5fed8b46344e9298d55427382ce573485a264a2ae287ed829e2f2548223e8', // 长青SVIP音源(二改修复版) v1.2.0
]);
function isTrustedFile(file) {
  try {
    const h = crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
    return ALLOWED_SOURCE_HASHES.has(h);
  } catch (_) { return false; }
}

// ========== 读取音源脚本 ==========
function loadScripts(file) {
  // 信任校验：默认只加载白名单音源；非白名单需显式 --allow-untrusted
  if (!allowUntrusted && !isTrustedFile(file)) {
    process.stderr.write('[lx-url-server] 跳过不受信任的音源: ' + file
      + '（如需加载请加 --allow-untrusted）\n');
    return [];
  }
  const raw = fs.readFileSync(file, 'utf-8');
  // 判断是 user_api.json 还是单个 JS
  try {
    const json = JSON.parse(raw);
    if (Array.isArray(json.userApis) && json.userApis.length) {
      // 取第一个可用的音源（按顺序）
      const items = json.userApis.map(api => ({
        name: api.name,
        version: api.version,
        description: api.description,
        author: api.author,
        homepage: api.homepage,
        script: api.script.startsWith('gz_')
          ? zlib.inflateSync(Buffer.from(api.script.slice(3), 'base64')).toString('utf-8')
          : api.script,
      }));
      return items;
    }
  } catch (e) { /* 不是 JSON，当作单脚本 */ }
  return [{ name: path.basename(file), version: '1.0.0', script: raw }];
}

// 加载优先级（2026-08-22 全流程实测保留的 5 个有效音源）：
// 聚合源候选最多放最前，其后是 QQ 直查稳定的源。运行时另有 sourceScore
// 成功榜动态调序，这里的静态排序只决定冷启动顺序。
const SOURCE_PRIORITY = ['墨澜', 'K×H', '玉宁熙', '屿溪', '长青'];
function sourceRank(name) {
  for (let i = 0; i < SOURCE_PRIORITY.length; i++) if (name.includes(SOURCE_PRIORITY[i])) return i;
  return SOURCE_PRIORITY.length;
}

function loadScriptSet() {
  if (apiFile) return loadScripts(apiFile);
  const dir = apiDir || path.join(__dirname, 'sources');
  const files = fs.readdirSync(dir).filter(name => /\.(js|json)$/i.test(name));
  files.sort((a, b) => (sourceRank(a) - sourceRank(b)) || a.localeCompare(b));
  return files.flatMap(name => loadScripts(path.join(dir, name)));
}
const scripts = loadScriptSet();

// ========== HTTP 请求 ==========
const MAX_RESPONSE_BYTES = 5 * 1024 * 1024; // 单次响应上限，防恶意/异常源拖垮内存
function doRequest(urlStr, opts = {}, timeout = 20000) {
  return new Promise((resolve, reject) => {
    let u;
    try { u = new URL(urlStr); } catch (e) { return reject(new Error('bad url: ' + urlStr)); }
    const lib = u.protocol === 'https:' ? https : http;
    const req = lib.request(u, {
      method: opts.method || 'GET',
      // 强制校验 TLS 证书：部分音源脚本会逃逸 vm 并置 NODE_TLS_REJECT_UNAUTHORIZED=0，
      // 显式指定可覆盖该 env，保证本进程自身的 HTTPS 连接不被降级（防中间人）。
      rejectUnauthorized: true,
      headers: Object.assign({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': u.origin + '/',
      }, opts.headers || {}),
      timeout,
    }, (res) => {
      const chunks = [];
      let total = 0;
      res.on('data', (c) => {
        total += c.length;
        if (total > MAX_RESPONSE_BYTES) {
          req.destroy(new Error('response too large'));
          return;
        }
        chunks.push(c);
      });
      res.on('end', () => {
        const raw = Buffer.concat(chunks);
        let body = raw.toString('utf-8');
        let parsed = null;
        try { parsed = JSON.parse(body); } catch (e) { parsed = body; }
        resolve({ statusCode: res.statusCode, headers: res.headers, raw, body: parsed });
      });
    });
    req.on('error', reject);
    req.on('timeout', () => req.destroy(new Error('timeout')));
    if (opts.body) req.write(typeof opts.body === 'string' ? opts.body : JSON.stringify(opts.body));
    req.end();
  });
}

// ========== 构建 lx 环境 ==========
function buildLx(apiInfo) {
  const handlers = { request: null };
  const lx = {
    EVENT_NAMES: { request: 'request', inited: 'inited', updateAlert: 'updateAlert' },
    request: (url, opts, cb) => {
      if (typeof opts === 'function') { cb = opts; opts = {}; }
      let cancelled = false;
      doRequest(url, opts)
        .then(res => { if (!cancelled) cb(null, res, res.body); })
        .catch(err => { if (!cancelled) cb(err, null, null); });
      return { abort() { cancelled = true; } };
    },
    send: (event, data) => {
      if (event === 'updateAlert') return Promise.resolve();
      if (event === 'inited') return Promise.resolve();
      return Promise.resolve();
    },
    on: (event, cb) => { if (event === 'request') handlers.request = cb; },
    utils: {
      crypto: {
        md5: (s) => crypto.createHash('md5').update(s).digest('hex'),
        aesEncrypt: (data, alg, key, iv) => { const c = crypto.createCipheriv(alg, Buffer.from(key), Buffer.from(iv)); return Buffer.concat([c.update(data), c.final()]); },
        rsaEncrypt: (data, key) => crypto.publicEncrypt({ key, padding: crypto.constants.RSA_NO_PADDING }, Buffer.concat([Buffer.alloc(128 - data.length), data])),
        randomBytes: (n) => crypto.randomBytes(n),
      },
      buffer: { from: (...a) => Buffer.from(...a), bufToString: (b, enc) => Buffer.from(b, 'binary').toString(enc) },
      zlib: {
        inflate: (d) => new Promise((res, rej) => zlib.inflate(d, (e, o) => e ? rej(e) : res(o))),
        deflate: (d) => new Promise((res, rej) => zlib.deflate(d, (e, o) => e ? rej(e) : res(o))),
      },
    },
    currentScriptInfo: {
      name: apiInfo.name || '',
      description: apiInfo.description || '',
      version: apiInfo.version || '',
      author: apiInfo.author || '',
      homepage: apiInfo.homepage || '',
      rawScript: apiInfo.script || '',
    },
    version: '2.0.0',
    env: 'desktop',
  };
  return { lx, handlers };
}

// ========== 初始化音源 ==========
let requestHandlers = [];
let initError = null;

async function init() {
  // 注意：音源脚本是不可信第三方代码，但现实中的落雪音源大量依赖
  // globalThis.lx、new Function/eval 等能力，过度加固会直接加载失败
  // （实测禁用 codeGeneration / 遮蔽 globalThis 会令全部源注册失败）。
  // 因此这里仅做白名单式的最小暴露，并把“vm 不是强安全边界”作为已知约束：
  // 生产上应仅加载经校验/哈希固定的可信音源，而非依赖 vm 隔离。
  for (const api of scripts) {
    const { lx, handlers } = buildLx(api);
    const context = {
      lx, window: { lx },
      console: { log: () => {}, error: () => {}, warn: () => {} },
      setTimeout, clearTimeout, Buffer,
      fetch, AbortSignal, URL, // 供音源做 URL 可下载性校验
    };
    vm.createContext(context);
    try {
      vm.runInContext(api.script, context, { timeout: 8000 });
    } catch (e) {
      initError = `${api.name}: 脚本执行失败 - ${e.message}`;
      continue;
    }
    // 等待处理器注册
    for (let i = 0; i < 40 && !handlers.request; i++) {
      await new Promise(r => setTimeout(r, 100));
    }
    if (handlers.request) {
      requestHandlers.push({ name: api.name || 'unknown', handler: handlers.request });
      process.stderr.write(`[lx-url-server] 音源已加载: ${api.name} (${api.version || ''})\n`);
      continue;
    }
    initError = `${api.name}: 未注册 request 处理器`;
  }
  return requestHandlers.length > 0;
}

// ========== URL 有效性验证（HEAD + 跟随重定向） ==========
function checkUrl(url, timeout = 4000) {
  // 很多音乐 CDN 禁止 HEAD，且网易云等会先返回 302 跳转到真实音频地址。
  // 这里 HEAD 失败/重定向时自动改 GET 并跟随跳转，避免把有效链接误判为失效。
  // 4s 超时 + 极小 Range 探测，加快多候选验证，避免验证拖慢取链接。
  return new Promise((resolve) => {
    const MAX_REDIRECTS = 5;
    const probe = (urlStr, redirectsLeft) => {
      let u;
      try { u = new URL(urlStr); } catch (e) { return resolve(false); }
      const lib = u.protocol === 'https:' ? https : http;
      const headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36',
        'Range': 'bytes=0-1023',
        'Accept': 'audio/*,*/*;q=0.8',
      };
      const req = lib.request(u, { method: 'HEAD', headers, timeout, rejectUnauthorized: true }, (res) => {
        const code = Number(res.statusCode || 0);
        // 处理 3xx 重定向
        if (code >= 300 && code < 400) {
          const loc = res.headers['location'];
          res.destroy();
          if (loc && redirectsLeft > 0) {
            let next = loc;
            try { next = new URL(loc, u).toString(); } catch (e) {}
            return probe(next, redirectsLeft - 1);
          }
          return resolve(false);
        }
        const ct = String(res.headers['content-type'] || '').toLowerCase();
        const audioLike = !ct || ct.includes('audio') || ct.includes('octet-stream') || ct.includes('mpeg') || ct.includes('mp4');
        const ok = (code >= 200 && code < 300) || code === 206 || code === 416;
        res.destroy();
        if (ok && audioLike) return resolve(true);
        // HEAD 结果不确定时，改用极小 Range GET 再试（GET 同样处理重定向）
        probeGet(urlStr, redirectsLeft);
      });
      req.on('error', () => probeGet(urlStr, redirectsLeft));
      req.on('timeout', () => { req.destroy(); probeGet(urlStr, redirectsLeft); });
      req.end();
    };
    const probeGet = (urlStr, redirectsLeft) => {
      let u;
      try { u = new URL(urlStr); } catch (e) { return resolve(false); }
      const lib = u.protocol === 'https:' ? https : http;
      const headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36',
        'Range': 'bytes=0-1023',
        'Accept': 'audio/*,*/*;q=0.8',
      };
      const req = lib.request(u, { method: 'GET', headers, timeout, rejectUnauthorized: true }, (res) => {
        const code = Number(res.statusCode || 0);
        if (code >= 300 && code < 400) {
          const loc = res.headers['location'];
          res.destroy();
          if (loc && redirectsLeft > 0) {
            let next = loc;
            try { next = new URL(loc, u).toString(); } catch (e) {}
            return probe(next, redirectsLeft - 1);
          }
          return resolve(false);
        }
        const ct = String(res.headers['content-type'] || '').toLowerCase();
        const audioLike = !ct || ct.includes('audio') || ct.includes('octet-stream') || ct.includes('mpeg') || ct.includes('mp4');
        const ok = (code >= 200 && code < 300) || code === 206 || code === 416;
        res.destroy();
        resolve(ok && audioLike);
      });
      req.on('error', () => resolve(false));
      req.on('timeout', () => { req.destroy(); resolve(false); });
      req.end();
    };
    probe(url, MAX_REDIRECTS);
  });
}

// ========== 按名搜索兜底（多接口，带歌名+歌手+时长校验） ==========
function normKw(s) {
  if (!s) return '';
  return String(s)
    .replace(/\(\s*Live\s*\)/gi, '')
    .replace(/\([^)]*\)/g, '')
    .replace(/（[^）]*）/g, '')   // 全角括号内容（如“萌妹进行曲（高阶萌妹成长指南）”）
    .replace(/\s+/g, '')
    .replace(/[!-/:-@\[-`{-~]/g, '')
    .trim().toLowerCase();
}
function nameMatch(a, b) {
  const na = normKw(a), nb = normKw(b);
  if (!na || !nb) return true;
  if (na === nb) return true;
  // 版本后缀场景：如 “萌妹进行曲” vs “萌妹进行曲（高阶萌妹成长指南）”
  // （normKw 已去掉全角括号，后缀文字保留）。短名若是长名的前缀则视为同曲，
  // 命中结果仍由下载器按时长+内容校验兜底，不会下错歌。
  const [short, long] = na.length <= nb.length ? [na, nb] : [nb, na];
  return long.startsWith(short);
}
function singerMatch(actual, expected) {
  if (!expected) return true;
  if (!actual) return false;
  const na = normKw(actual), nb = normKw(expected);
  // 完全一致 或 相互包含（多歌手场景）
  if (na === nb || na.includes(nb) || nb.includes(na)) return true;
  // 多歌手：按分隔符拆分后逐名匹配（如“熊田茜音、増井優花” vs “増井優花;熊田茜音”）
  const split = (s) => s.split(/[、,，;;&/]/).map(x => normKw(x)).filter(Boolean);
  const ea = split(nb), aa = split(na);
  if (ea.length > 1) return ea.every(x => aa.some(y => y.includes(x) || x.includes(y)));
  return false;
}
// 时长匹配：期望 interval（秒）存在时，候选时长差异需在容差内，进一步防止下错歌
function intervalMatch(actualSec, expectedInterval) {
  if (!expectedInterval) return true;
  if (actualSec == null || isNaN(actualSec)) return true;
  const exp = Number(expectedInterval);
  if (!exp || isNaN(exp)) return true;
  return Math.abs(actualSec - exp) <= 15;
}
function songIntervalSeconds(song) {
  const v = song && (song.duration || song.interval || song.durationTime || (song.interval && song.interval.seconds));
  if (v == null) return null;
  const n = Number(v);
  return isNaN(n) ? null : n;
}

// 从任意音源 musicSearch 返回结果中提取歌曲数组（兼容多种返回格式）
function extractSongList(result) {
  if (!result) return [];
  let list = result;
  if (Array.isArray(result)) return result;
  if (typeof result === 'object') {
    if (Array.isArray(result.data)) list = result.data;
    else if (Array.isArray(result.list)) list = result.list;
    else if (Array.isArray(result.songs)) list = result.songs;
    else if (result.data && typeof result.data === 'object') {
      if (Array.isArray(result.data.list)) list = result.data.list;
      else if (Array.isArray(result.data.songs)) list = result.data.songs;
      else if (Array.isArray(result.data.data)) list = result.data.data;
    }
  }
  return Array.isArray(list) ? list : [];
}
function songTitle(song) {
  if (!song) return '';
  return song.title || song.name || song.songName || song.songname || '';
}
function songSinger(song) {
  if (!song) return '';
  const s = song.singer || song.artist || song.singers || song.songer;
  if (Array.isArray(s)) return s.map(x => (typeof x === 'string' ? x : x && (x.name || x.singerName))).filter(Boolean).join('、');
  if (typeof s === 'string') return s;
  if (s && typeof s === 'object') return s.name || '';
  return '';
}
function songMatch(song, name, singer, interval) {
  if (!song) return false;
  const t = songTitle(song);
  const s = songSinger(song);
  return nameMatch(t, name) && singerMatch(s, singer) && intervalMatch(songIntervalSeconds(song), interval);
}

// 根据 URL 判断是否疑似 128k（酷我 M500=128k、QQ C400/M500=128k、bitrate$128 等），避免源脚本内部降级到 128k
function urlLooks128k(url) {
  if (!url) return false;
  const u = String(url);
  if (/bitrate\$?128|bitrate=128|rate=128|br=128|size=128/i.test(u)) return true;
  // 酷我：M500 段=128k、M800=320k、F000=flac；QQ：C400/M500=128k、M800=320k、F000/Q000=无损
  if (/(^|[/._-])M500[0-9A-Za-z]/.test(u)) return true;
  if (/(^|[/._-])C400[0-9A-Za-z]/.test(u)) return true;
  return false;
}
// 根据 URL 判断是否疑似 320k（酷我 M800 段、QQ M800）
function urlLooks320(url) {
  if (!url) return false;
  return /(^|[/._-])M800[0-9A-Za-z]/.test(String(url));
}
// 根据 URL 判断是否疑似无损（酷我 F000 段、QQ F000/Q000）
function urlLooksLossless(url) {
  if (!url) return false;
  return /(^|[/._-])(F000|Q000)[0-9A-Za-z]/.test(String(url));
}

// ========== 平台级并发限制 ==========
// 多 worker 并发下载时，同一音乐平台的 API 会被瞬间打爆触发限流
// （表现为所有音源同时"获取链接失败"）。这里按目标平台限制并发数：
// 同一平台同时最多 PLATFORM_MAX_CONCURRENT 个请求，从源头避免把平台打挂。
// 3 路在“不触发限流”与“够快”之间取平衡；若仍被限流可调低。
const PLATFORM_MAX_CONCURRENT = 3;
const platformActive = {};
// 统一调用入口：带平台并发限制 + 超时兜底，替代所有裸 entry.handler.call(...)
async function callHandler(entry, source, action, info, timeoutMs) {
  const key = source || 'kw';
  while ((platformActive[key] || 0) >= PLATFORM_MAX_CONCURRENT) {
    await new Promise((r) => setTimeout(r, 60));
  }
  platformActive[key] = (platformActive[key] || 0) + 1;
  try {
    const timer = new Promise((_, rej) => setTimeout(() => rej(new Error('取链接超时')), timeoutMs));
    const p = Promise.resolve().then(() => entry.handler.call(null, { source, action, info }));
    return await Promise.race([p, timer]);
  } finally {
    platformActive[key] -= 1;
  }
}

// 用某个音源对"候选歌曲对象"取播放链接（走该源自己的 musicUrl）
async function getUrlFromSourceEntry(entry, source, songInfo, quality) {
  try {
    const result = await callHandler(entry, source, 'musicUrl', { musicInfo: songInfo, type: quality }, 5000);
    let url = null;
    if (typeof result === 'string' && /^https?:\/\//.test(result)) url = result;
    else if (result && typeof result === 'object') {
      url = result.url || (result.data && result.data.url) || (result.url && result.url.url);
    }
    if (url && /^https?:\/\//.test(url)) {
      // 明确只要 320k+：返回的 URL 是已知 128k 时直接放弃，避免把低质当高质
      if (urlLooks128k(url) && !urlLooks320(url) && !urlLooksLossless(url)) return null;
      const valid = await checkUrl(url);
      if (valid) return url;
    }
    return null;
  } catch (e) {
    return null;
  }
}

// QQ 音乐搜索（带重试）：瞬时限流会返回空结果，重试几次通常能恢复
async function qqSearch(kw) {
  const url = `https://c.y.qq.com/soso/fcgi-bin/client_search_cp?p=1&n=10&w=${encodeURIComponent(kw)}&format=json&cr=1&t=0`;
  for (let i = 0; i < 2; i++) {
    try {
      const r = await doRequest(url, { method: 'GET' }, 12000);
      const list = (r.body && r.body.data && r.body.data.song && r.body.data.song.list) || [];
      if (list.length) return list;
    } catch (e) { /* continue */ }
    await new Promise((res) => setTimeout(res, 1000));
  }
  return [];
}

async function searchByName(name, singer, interval) {
  // 收集多个候选（而不是只返回 1 个），供下载器逐个实测内容后挑达标的：
  // URL 特征（M500/F000/level=lossless 等）只能猜码率，代理型链接（如
  // mcp.nianxinxz.com/.../tx.php?level=lossless）常被标成无损却返回 48k 假文件，
  // 因此必须把候选都带回 Python，下载后按真实内容校验。
  const candidates = [];           // {url, quality, via, score}
  const seenUrls = new Set();
  const addCandidate = (url, quality, via, score) => {
    if (!url || seenUrls.has(url)) return;
    seenUrls.add(url);
    candidates.push({ url, quality, via, score });
  };
  // 目标：无损 > 320k > 其他；320k 已是可接受下限，128k 一律不要
  const qOrder = ['flac24bit', 'flac', '320k'];
  const scoreOf = (qi, url) => {
    let s = qi * 10;
    if (urlLooksLossless(url)) s -= 5;
    else if (urlLooks320(url)) s += 1;
    else if (urlLooks128k(url)) s += 50; // 已知 128k，几乎垫底
    return s;
  };
  // 兜底搜索允许的总耗时预算：源一多（约 30 个）逐个带超时轮询会很慢，
  // 一旦超时就用已收集到的候选返回，宁可少几个候选也不能让下载卡死。
  const deadline = Date.now() + 30000;
  const overBudget = () => Date.now() > deadline;

  // 方式一：多平台逐音源 musicSearch（换接口搜索）—— 精确命中后取直链
  for (const platform of ['kw', 'kg', 'wy', 'tx']) {
    if (overBudget()) break;
    for (const entry of requestHandlers) {
      if (overBudget()) break;
      if (sourceBlocked(entry.name)) continue;
      try {
        const result = await callHandler(entry, platform, 'musicSearch', { musicInfo: { name, singer }, page: 1, type: 'song' }, 5000);
        const list = extractSongList(result);
        for (const song of list) {
          if (!songMatch(song, name, singer, interval)) continue;
          // 命中的歌曲可能来自其它平台，取该歌自身的 source 字段（缺省沿用搜索平台）
          const hitSource = song.source || song.platform || song.from || platform;
          // 组装该源 musicUrl 所需的 musicInfo（尽量保留 song 原始字段 + id 兼容字段）
          const musicInfo = Object.assign({}, song, { name: songTitle(song), singer: songSinger(song) });
          if (musicInfo.id == null && musicInfo.songId != null) musicInfo.id = musicInfo.songId;
          for (let qi = 0; qi < qOrder.length; qi++) {
            const q = qOrder[qi];
            const url = await getUrlFromSourceEntry(entry, hitSource, musicInfo, q);
            if (!url) continue;
            addCandidate(url, q, `${entry.name}(${platform}搜索)`, scoreOf(qi, url));
          }
        }
        if (list.length) noteSourceOk(entry.name);
      } catch (e) { /* 该源不支持/搜索失败，继续下一个 */ }
    }
  }

  // 方式二：溯音酷我（oiapi）严格校验
  try {
    const kwQuery = encodeURIComponent(`${singer} ${name}`);
    const r = await doRequest(`https://oiapi.net/api/Kuwo?msg=${kwQuery}&n=1&br=5`, { method: 'GET' }, 15000);
    if (r.statusCode === 200 && r.body && typeof r.body === 'object' && r.body.code === 1) {
      const m = r.body.message || '';
      const parsed = {};
      for (const line of String(m).split('\n')) {
        if (line.startsWith('歌名：')) parsed.song = line.replace('歌名：', '').trim();
        if (line.startsWith('歌手：')) parsed.singer = line.replace('歌手：', '').trim();
      }
      const urlMatch = m.match(/音乐链接[：:](\S+)/);
      if (urlMatch && /^https?:\/\//.test(urlMatch[1])) {
        if (nameMatch(parsed.song, name) && singerMatch(parsed.singer, singer) && !urlLooks128k(urlMatch[1])) {
          const url = urlMatch[1];
          const valid = await checkUrl(url);
          if (valid) addCandidate(url, '320k', '溯音酷我(按名搜索)', 11);
        }
      }
    }
  } catch (e) { /* continue */ }
  // 方式三：溯音网易云（oiapi）严格校验
  try {
    const nameQuery = encodeURIComponent(`${singer} ${name}`);
    const r = await doRequest(`https://oiapi.net/api/Music_163?name=${nameQuery}&n=1&br=5`, { method: 'GET' }, 15000);
    if (r.statusCode === 200 && r.body && typeof r.body === 'object' && r.body.code === 0 && r.body.data) {
      const d = r.body.data;
      const dName = d.name || '';
      const dSingers = Array.isArray(d.singers) ? d.singers.map(s => s.name || '').join('、') : '';
      if (nameMatch(dName, name) && singerMatch(dSingers, singer) && intervalMatch(d.duration, interval) && !urlLooks128k(d.url)) {
        const url = d.url;
        if (url && /^https?:\/\//.test(url)) {
          const valid = await checkUrl(url);
          if (valid) addCandidate(url, '320k', '溯音网易云(按名搜索)', 12);
        }
      }
    }
  } catch (e) { /* continue */ }
  // 方式四：QQ 音乐按名搜索 → 把命中歌曲的 songmid 喂回各音源 tx 取链接。
  // 兜底场景：某些源自身的 tx 搜索返回 128k，但直接喂正确 songmid 时可拿到 M800/F000。
  try {
    const list = await qqSearch(`${singer} ${name}`);
    for (const hit of list) {
      const mid = hit.songmid || '';
      if (!mid) continue;
      const qTitle = hit.songname || '';
      const qSinger = (hit.singer || []).map(x => x.name || '').join('、');
      // 歌名必须匹配；歌手尽量匹配（QQ 常用中文译名，如“久保由利香” vs “久保ユリカ”）。
      // 歌手不一致但歌名+时长一致时也收录（稍降优先级），最终由下载器下载后按内容/时长校验兜底。
      if (!nameMatch(qTitle, name)) continue;
      if (!intervalMatch(Number(hit.interval), interval)) continue;
      // 歌手匹配与否：明显不匹配（如 DECO*27 vs 柊木りお 的翻唱版）时大幅劣后，
      // 让原唱结果排前面；不再仅仅“+2”。QQ 常用中文译名场景仍靠 nameMatch 放宽。
      const singerOk = singerMatch(qSinger, singer);
      const singerPenalty = singerOk ? 0 : 20;
      const musicInfo = { name: qTitle, singer: qSinger, songmid: mid, id: mid };
      for (const qi of [0, 1, 2]) {
        const q = qOrder[qi];
        for (const entry of requestHandlers) {
          if (overBudget()) break;
          if (sourceBlocked(entry.name)) continue;
          const url = await getUrlFromSourceEntry(entry, 'tx', musicInfo, q);
          if (!url) continue;
          addCandidate(url, q, `${entry.name}(QQ直查mid)`, scoreOf(qi, url) + singerPenalty);
        }
        if (overBudget()) break;
      }
      if (overBudget()) break;
    }
  } catch (e) { /* continue */ }

  // 方式五：已有候选都拿不到高音质时，尝试只用歌名搜索（不带歌手名）。
  // 个别歌曲的歌手名在不同平台差异较大（日文 vs 中文译名 vs 罗马音），
  // 带歌手搜索可能无结果；此时哪怕已有 128k 候选，也应继续找无损/320k。
  const hasHighQuality = candidates.some((c) => urlLooks320(c.url) || urlLooksLossless(c.url));
  if (singer && !hasHighQuality) {
    try {
      const list = await qqSearch(name);
      for (const hit of list) {
        if (overBudget()) break;
        const mid = hit.songmid || '';
        if (!mid) continue;
        const qTitle = hit.songname || '';
        const qSinger = (hit.singer || []).map(x => x.name || '').join('、');
        if (!nameMatch(qTitle, name)) continue;
        if (!intervalMatch(Number(hit.interval), interval)) continue;
        const musicInfo = { name: qTitle, singer: qSinger, songmid: mid, id: mid };
        for (const qi of [0, 1, 2]) {
          const q = qOrder[qi];
          for (const entry of requestHandlers) {
            if (overBudget()) break;
            if (sourceBlocked(entry.name)) continue;
            const url = await getUrlFromSourceEntry(entry, 'tx', musicInfo, q);
            if (!url) continue;
            // 无歌手搜索最易命中翻唱，统一加高罚分排最后，靠原唱结果优先
            addCandidate(url, q, `${entry.name}(QQ无歌手搜索)`, scoreOf(qi, url) + 20);
          }
          if (overBudget()) break;
        }
      }
    } catch (e) { /* continue */ }
  }

  if (!candidates.length) throw new Error(`按名搜索失败: ${singer} - ${name}`);
  candidates.sort((a, b) => a.score - b.score);
  const top = candidates.slice(0, 12).map(({ url, quality, via }) => ({ url, quality, via }));
  return { url: top[0].url, quality: top[0].quality || '320k', via: top[0].via, candidates: top };
}

// ========== 获取播放链接（多候选，带音源级冷却） ==========
// 某个音源连续多次拿不到有效链接时，暂时跳过它（冷却），避免死源反复拖慢整体。
const sourceCooldown = new Map(); // name -> { fails, until }
const COOLDOWN_FAILS = 3;   // 连续失败多少次进入冷却
const COOLDOWN_MS = 3 * 60 * 1000; // 冷却时长 3 分钟（网络抖动/限流不计入冷却）

function sourceBlocked(name) {
  const c = sourceCooldown.get(name);
  return !!(c && c.until > Date.now());
}
function noteSourceOk(name) { sourceCooldown.delete(name); }
function noteSourceFail(name, transient = false) {
  const c = sourceCooldown.get(name) || { fails: 0, until: 0 };
  if (transient) {
    // 网络抖动/平台限流等瞬时错误：不累计进冷却，避免把好音源也关进小黑屋
    c.fails = Math.max(0, c.fails - 1);
  } else {
    c.fails += 1;
    if (c.fails >= COOLDOWN_FAILS) { c.until = Date.now() + COOLDOWN_MS; c.fails = 0; }
  }
  sourceCooldown.set(name, c);
}

// ========== 音源成功榜（剪枝核心） ==========
// 每次取链接都遍历全部音源直到撞 deadline 会很慢。维护一个"近期成功率"排序，
// 每次只先试排序靠前的音源，拿够候选就提前停止，把时间花在好源上。
const sourceScore = new Map(); // name -> score（越大越优先）
function rankedHandlers() {
  return requestHandlers.slice().sort((a, b) => {
    const sa = sourceScore.get(a.name) || 0;
    const sb = sourceScore.get(b.name) || 0;
    return sb - sa;
  });
}
function bumpSource(name, delta) {
  const cur = sourceScore.get(name) || 0;
  sourceScore.set(name, Math.min(10, Math.max(-5, cur + delta)));
}

async function getMusicUrls(source, musicInfo, qualities) {
  if (!requestHandlers.length) throw new Error('音源未初始化: ' + (initError || 'unknown'));
  // 尝试目标音质，按"音源成功榜"优先尝试，拿够候选即停止（避免试满全部音源直到超时）
  const qList = Array.isArray(qualities) ? qualities : [qualities];
  const seen = new Set();
  const candidates = [];
  const targetCount = 2; // 2 个候选即够 Python 端实测（实测比多候选更省时）
  let lastErr = null;
  // 整体取链接预算：超时就返回已收候选（即使没拿够），绝不让一首歌卡死
  const dlDeadline = Date.now() + 12000;
  const overDl = () => Date.now() > dlDeadline;
  for (const q of qList) {
    for (const entry of rankedHandlers()) {
      if (overDl()) break;
      if (sourceBlocked(entry.name)) continue;
      try {
        const info = { musicInfo: { ...musicInfo }, type: q };
        const result = await callHandler(entry, source, 'musicUrl', info, 5000);
        let url = null;
        if (typeof result === 'string' && /^https?:\/\//.test(result)) url = result;
        else if (result && typeof result === 'object') {
          url = result.url || (result.data && result.data.url) || (result.url && result.url.url);
        }
        if (url && /^https?:\/\//.test(url) && !seen.has(url)) {
          // 下载器明确禁止 128k，忽略音源脚本内部回退出的 128k 结果。
          const resultQuality = result && typeof result === 'object'
            ? String(result.quality || result.level || result.br || '')
            : '';
          if (q === '128k' || resultQuality.toLowerCase() === '128k' || resultQuality === '128') {
            lastErr = new Error('已忽略 128k 音源结果');
            continue;
          }
          seen.add(url);
          const valid = await checkUrl(url);
          if (valid) {
            candidates.push({ url, quality: q, via: entry.name });
            noteSourceOk(entry.name);
            bumpSource(entry.name, 3);
            if (candidates.length >= targetCount) break; // 拿够候选立即停止
          } else {
            lastErr = new Error('链接无效(404/403): ' + url.slice(0, 100));
            noteSourceFail(entry.name, true);
            bumpSource(entry.name, -3);
          }
        } else {
          lastErr = new Error('音源返回无效链接: ' + JSON.stringify(result).slice(0, 120));
          noteSourceFail(entry.name, true);
          bumpSource(entry.name, -1);
        }
      } catch (e) {
        lastErr = e;
        // 超时/网络错误可能是平台限流导致，不记入冷却（以免误把好源锁死）
        const isTimeout = String(e.message || e).includes('超时');
        noteSourceFail(entry.name, !isTimeout);
        bumpSource(entry.name, isTimeout ? -2 : -1);
      }
    }
    if (candidates.length > 0) break; // 已有候选就不到更高音质档继续扫源
  }
  if (!candidates.length) throw lastErr || new Error('获取链接失败');
  return candidates;
}

// ========== 主流程 ==========
(async () => {
  const ok = await init();
  if (!ok) {
    process.stderr.write('[lx-url-server] 所有音源加载失败: ' + (initError || '') + '\n');
    process.exit(1);
  }

  const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  rl.on('line', async (line) => {
    let msg;
    try { msg = JSON.parse(line); } catch (e) {
      process.stdout.write(JSON.stringify({ id: null, ok: false, error: 'bad json' }) + '\n');
      return;
    }
    try {
      if (msg.cmd === 'url') {
        const cands = await getMusicUrls(msg.source, msg.musicInfo || {}, msg.quality || '320k');
        process.stdout.write(JSON.stringify({ id: msg.id, ok: true, candidates: cands }) + '\n');
      } else if (msg.cmd === 'searchByName') {
        const r = await searchByName(msg.name, msg.singer || '', msg.interval);
        process.stdout.write(JSON.stringify({ id: msg.id, ok: true, ...r }) + '\n');
      } else if (msg.cmd === 'scanSources') {
        // 调试/健康检查：对若干歌曲逐个音源实测，返回每个音源成功命中的歌曲数
        const songs = msg.songs || [];
        const perSource = {};
        const report = [];
        for (const song of songs) {
          const info = { musicInfo: { ...(song.musicInfo || {}) }, type: song.quality || '320k' };
          const songRes = { name: song.name, singer: song.singer, wins: [], detail: [] };
          for (const entry of requestHandlers) {
            if (sourceBlocked(entry.name)) continue;
            try {
              const result = await callHandler(entry, song.source || 'kw', 'musicUrl', info, 5000);
              let url = null;
              if (typeof result === 'string' && /^https?:\/\//.test(result)) url = result;
              else if (result && typeof result === 'object') {
                url = result.url || (result.data && result.data.url) || (result.url && result.url.url);
              }
              if (url && /^https?:\/\//.test(url)) {
                const valid = await checkUrl(url);
                if (valid) { songRes.wins.push(entry.name); perSource[entry.name] = (perSource[entry.name] || 0) + 1; noteSourceOk(entry.name); }
                else noteSourceFail(entry.name);
                songRes.detail.push({ src: entry.name, url: url.slice(0, 80), ok: valid });
              } else {
                noteSourceFail(entry.name);
                songRes.detail.push({ src: entry.name, raw: JSON.stringify(result).slice(0, 60), ok: false });
              }
            } catch (e) { noteSourceFail(entry.name); songRes.detail.push({ src: entry.name, err: String(e.message || e).slice(0, 40), ok: false }); }
          }
          report.push(songRes);
        }
        process.stdout.write(JSON.stringify({ id: msg.id, ok: true, report, perSource }) + '\n');
      } else if (msg.cmd === 'searchList') {
        // 调试：多平台列出所有命中的候选歌曲（不含直链），用于排查 320k 是否真的不存在
        const hits = [];
        for (const platform of ['kw', 'kg', 'wy', 'tx']) {
          for (const entry of requestHandlers) {
            if (sourceBlocked(entry.name)) continue;
            try {
              const result = await Promise.race([
                entry.handler.call(null, {
                  source: platform, action: 'musicSearch',
                  info: { musicInfo: { name: msg.name, singer: msg.singer || '' }, page: 1, type: 'song' },
                }),
                new Promise((_, rej) => setTimeout(() => rej(new Error('搜索超时')), 12000)),
              ]);
              const list = extractSongList(result);
              for (const song of list) {
                if (!nameMatch(songTitle(song), msg.name)) continue;
                const src = song.source || song.platform || song.from || platform;
                const qs = song.quality || song.qualitys || song._qualitys || {};
                let qStr = '';
                if (Array.isArray(qs)) qStr = qs.join(',');
                else if (typeof qs === 'object') qStr = Object.keys(qs).join(',');
                else if (qs) qStr = String(qs);
                hits.push({
                  platform, via: entry.name, src,
                  name: songTitle(song), singer: songSinger(song),
                  interval: songIntervalSeconds(song), qualitys: qStr,
                  id: song.id || song.songId || song.songmid || '',
                });
              }
              if (list.length) noteSourceOk(entry.name);
            } catch (e) { /* skip */ }
          }
        }
        process.stdout.write(JSON.stringify({ id: msg.id, ok: true, hits }) + '\n');
      } else if (msg.cmd === 'ping') {
        process.stdout.write(JSON.stringify({ id: msg.id, ok: true, pong: true }) + '\n');
      } else {
        process.stdout.write(JSON.stringify({ id: msg.id, ok: false, error: 'unknown cmd: ' + msg.cmd }) + '\n');
      }
    } catch (e) {
      process.stdout.write(JSON.stringify({ id: msg.id, ok: false, error: String(e.message || e) }) + '\n');
    }
  });
  process.stdout.write(JSON.stringify({ ready: true }) + '\n');
})();
