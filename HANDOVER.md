# 交接文档（HANDOVER）

> 更新时间：2026-10-31 14:10
> 仓库：`C:\Users\God Know\Documents\code\lx-music-download`
> 远端：https://github.com/MyGodKnow/lx-music-downloader（分支 `main`）
> 状态：**已提交，版本 1.2.0，EXE 已重新构建**

> ⚠️ 本文件第一~十章写于 2026-10-18（v0.2.0），其中版本号、目录结构、
> 待办事项多数已过时。**请优先阅读下面的「第十一章：2026-10-31 下载失败排查」**，
> 那是当前最新的状态与结论。

---

## 十一、2026-10-31 下载失败排查（本轮）

### 结论：修复 7 个根因，实测成功率 40% → 67%

同一批 15 首真实歌单（请求 `flac`、最低 `320k`）：

| 指标 | 修复前 | 修复后 |
|------|--------|--------|
| 成功 | 6 | **10** |
| 失败 | 9 | 5 |
| 真无损 | 4 | **9** |

### 7 个 bug（按影响排序）

| # | 问题 | 位置 | 后果 |
|---|------|------|------|
| 1 | 平台加密容器未识别 | `is_encrypted_audio_url()` / `is_encrypted_audio_file()` / `urlLooksEncrypted()` | 酷我 `.mgg`/`.mflac`、QQ `.qmc*` 被当普通音频下载，解析不出时长→「时长校验失败」，每次白下几十 MB |
| 2 | 替代源 SQL 条件互斥 | `get_alternative_sources()` | `name != 当前歌名` 与后续「严格同名」比对矛盾，多平台兜底**永远返回空** |
| 3 | 按名搜索兜底不走降级 | `_process_song_inner()` 兜底分支 | 请求 flac 时搜到可用 320k 也判不达标，整首失败 |
| 4 | 酷狗各档 hash 混用 | `get_music_info(row, quality)` | meta 顶层 hash 固定是 128k，未按档位覆盖，请求 flac 仍返回 128k |
| 5 | `directFallbackCandidates` 被误删 | `lx_url_server.js` | 8522b87 重构时删了函数体但留下调用，异常被 catch 吞掉→兜底**静默失效** |
| 6 | `targetCount` 成死代码 | `getMusicUrls()` | 命中 1 个候选就 `break`，唯一候选是试听片段时无备选 |
| 7 | 音质标签虚高 | `detect_actual_quality()` | 照抄音源自报值，16bit FLAC 显示成 `[flac24bit]` |

### 3 项优化

- **跨档位复用**：`download_with_candidates(..., reuse=)` —— flac 档因音质不足被拒的文件，
  降到 320k 时直接复用，不再整首重下。
- **失败清单存 id/meta**：重试时直查原平台，不依赖经常不可用的搜索接口（老清单兼容）。
- **错误信息去重**：修掉 `320k: 320k: ...`。

### 关键实测数据（排查时可直接复用）

- 加密容器 `.mgg` 文件头：`29 01 55 61 70 31 50 34 71 37 35 66 37 32`（`\x29\x01` + `Uap1P4q75f72`）
- 酷我可用直链形态：`.../trackmedia/M500...mp3`（128k）、`M800...mp3`（320k）、`F000....flac`（无损）
- 网易云 `music.163.com/song/media/outer/url` 对无版权曲目返回 `application/json` 错误页，不是音频
- 本机音源现状：`K×H测试.js` / `K×H测试-2.js` 较稳；`星海音乐源.js` 常报「缺少 songId」；
  `长青SVIP` 报「缺失歌曲标识」；`独家音源.js` 报 `block ip`；`墨澜聚合音源.js` 常给 10-20s 试听片段

### 剩余失败的性质（不是 bug）

1. **音源只提供 128k**：`--min-quality` 默认 `320k`，低于此整首记失败（见待办 1）
2. **音源返回他人翻唱**：被「歌手标签校验」正确拦截，属于保护行为
3. **音源返回错误歌曲**：如 `K×H测试-2.js` 对多首歌返回同一首（时长差 35s 被拒）

### 本轮验证命令

```bash
cd lx-downloader
py -m unittest discover -s tests        # 118 passed
node --check lx_url_server.js           # 语法
cd ../lx-tauri && npx tsc --noEmit       # 0 error
```

> ⚠️ **测试坑**：不要用 `python -I` 跑引擎——隔离模式会屏蔽用户 site-packages，
> 导致 `mutagen` 缺失，所有音质校验静默返回 False，看起来像「全部音质不达标」。
> 本机 `python` 指向 WindowsApps 存根（退出码 49），请用 `py`。

### 打包（本轮已执行）

```bash
cd lx-downloader
py -m PyInstaller --noconfirm --clean lx_music_downloader.spec
# 把 dist/lx_music_downloader/ 打成 zip 覆盖 lx-tauri/src-tauri/engine/engine.zip
cd ../lx-tauri && npm run tauri build -- --no-bundle
# 复制 src-tauri/target/release/lx-tauri.exe → 仓库根「落雪音乐下载器.exe」
```

### 待办（优先级）

1. **[需产品决策] 降级下限**：当前 `min-quality=320k`，音源只有 128k 时整首失败。
   若要「宁可低质也要有歌」，把默认改成 `128k`（`App.tsx` 的 `useState("320k")`
   与引擎 `--min-quality` 的 `default`）。
2. **`/gh` 音源脚本质量**：多数失败源于音源本身（返回错歌/试听片段/缺 ID），
   程序侧只能校验拒绝，无法修复。
3. 若某天出现「所有音源都失败」，优先看 `.health.json` 与 `--probe-sources`。

---

## 〇、当前状态速览（2026-10-18 更新）

| 项目 | 状态 |
|------|------|
| 提交 | ✅ 已提交 5 个 commit（`040c36e` … `7879bea`） |
| EXE | ✅ 已用新图标重新编译（`落雪音乐下载器.exe`，55.2 MB，08:50） |
| 图标 | ✅ 新设计稿（八分音符 + 雪花）已嵌入 exe，验证过 32px 提取结果 |
| 头像 | ✅ 关于页显示作者 GitHub 头像 |
| 测试 | ✅ Python 46 项、Rust 27 项、clippy 0 警告、tsc 0 错误 |
| 引擎 | ✅ `engine.zip` 哈希与已释放引擎一致 |

> ⚠️ **踩坑记录**：Tauri 增量编译时**不会**重新嵌入 exe 图标资源。
> 换了 `icons/*` 之后必须删掉 `src-tauri/target/release/lx-tauri.exe` 和
> `target/release/build` 再编译，否则 exe 里仍是旧图标。

---

## 一、项目概览

落雪音乐下载器：读取落雪音乐桌面版歌单，多音源批量下载无损音乐的 Windows 工具。
架构为 **Tauri 2 (React 19 + Rust) 外壳 + Python 引擎 + Node 音源桥接**，
最终交付单文件 EXE（引擎以 zip 形式编译期内嵌）。

```
落雪 DB / .lxmc ──┐
                  ├─► Python 引擎 ──stdin/stdout JSON-RPC──► Node 桥接 ──► 音源脚本 ──► 平台 API
%APPDATA%\lx-      │      │
downloader-sources ┘      └─► 四重校验（时长/版本词/歌手/音质）──► 落盘
```

---

## 二、本轮完成的工作

### A. Bug 修复（5 项）

| # | 问题 | 修复位置 |
|---|------|----------|
| 1 | 128k/192k 档位必然失败：音质校验恒定要求「无损或 ≥320k」 | `lx_music_downloader.py` 的 `verify_quality(path, quality)`、`QUALITY_MIN_BITRATE`、`lower_quality_options` |
| 2 | 「停止」是硬杀，`.part` 残留、`failed.txt` 不写 | 新增 `--stop-file` 信号文件机制；`lib.rs::stop_task` 改为先写信号、等 20s、超时才 `taskkill /T /F` |
| 3 | 配置自愈顺序错误：先 `mkdir` 再判乱码，反而建出乱码目录 | `_dir_is_usable()` 重写，乱码检测前置且不得创建目录 |
| 4 | 引擎升级可能卡在旧版本（zip 长度不变） | `lib.rs::engine_version()` 改 SHA-256 + 目录交换升级 |
| 5 | Node 桥接无条件丢弃 128k 结果 | `lx_url_server.js` 的 `getMusicUrls`，改为按 `qualityRank` 只在请求档位更高时丢弃 |

### B. 新功能（4 项，均已完成并实测）

#### 1. 搜索与单曲下载
- **前端**：新增「搜索单曲」页（`App.tsx`，Tab = `search`）；歌单卡片可展开歌曲列表（`song-table`）
- **引擎**：新增 `--search` / `--search-limit` / `--list-songs` / `--song-file` 四种模式
  - `run_search(args)`：输出 JSON 候选数组
  - `load_song_file(path)`：解析 GUI 传入的歌曲 JSON
  - `run_download(args, songs, db_path, title)`：歌单下载与单曲下载**共用**的下载管线
- **Rust**：`search_songs` / `list_songs` / `download_songs`，另有辅助函数 `run_engine_json`
- **Node**：新增 `searchSongs` 命令

> ⚠️ **重要背景**：你本机 5 个音源脚本的 `musicSearch` 接口**全部失效**（`searchList`、`searchByName` 都返回空），
> 但取直链 `musicUrl` 仍正常。因此搜索做了**不依赖音源脚本的直连兜底**：
> - `kuwoSearch()`：`http://search.kuwo.cn/r.s`（酷我老接口，返回单引号 JSON，需清洗 `&nbsp;` / `\&` / `\u0026`）
> - `neteaseSearch()`：`oiapi.net/api/Music_163`
> - `qqSearch()`：QQ 接口**当前返回 500**，仅作最后兜底
> - 结果按「歌名命中 + 歌手命中 + 版本词」做相关度排序（`scoreOf`）

#### 2. 一键导入落雪音乐桌面端音源
- **Rust**：`find_lx_desktop_api()` 探测 `user_api.json`；`import_lx_desktop_sources(path)` 批量导入
- **解码**：`decode_lx_script()` 处理 `gz_` 前缀 = `base64(zlib(deflate(js)))`
- 依赖新增：`base64 = "0.22"`、`flate2 = "1"`
- **实测**：你本机 `%APPDATA%\lx-music-desktop\LxDatas\user_api.json` 有 2 个音源
  （野草🌾 7148 字符、六音音源 332686 字符），解码结果与 Python 独立实现一致
- 文件名用 `sanitize_file_stem()` 清理，重名自动加 `-2`、`-3`

#### 3. 关于页（作者信息）
- **前端**：新增「关于」页（Tab = `about`），含作者、QQ、邮箱、GitHub Issues
- 一键复制（`copyText`）、外链打开（`openUrl`，来自 `@tauri-apps/plugin-opener`）
- 权限已确认：`opener:default` 的 `allow-default-urls` 覆盖 `mailto:` / `https://`，无需改 `capabilities`
- 常量集中在 `App.tsx` 顶部：
  ```ts
  const AUTHOR = {
    name: "MyGodKnow",
    qq: "1973490065",
    email: "114514woguizhidaoya@gmail.com",
    repo: "https://github.com/MyGodKnow/lx-music-downloader",
    issues: "https://github.com/MyGodKnow/lx-music-downloader/issues",
  };
  ```

#### 4. 作者头像
- 来源：`https://github.com/MyGodKnow.png`（460×460 JPEG，蓝紫色调）
- 资源：`lx-tauri/public/avatar/author-460.jpg`、`author-256.jpg`
- 样式：圆形头像 + 右下角品牌角标（`.about-avatar-wrap` / `.about-avatar` / `.about-avatar-badge`）

### C. 图标
- 母版：`lx-tauri/scripts/_source_icon.webp`（2048×2048，你提供的设计稿）
- 生成脚本：`lx-tauri/scripts/gen_icons.py`，**已重写为「从母版缩放」模式**（不再程序化绘制）
- 产物：14 个 PNG + `icon.ico`（16/24/32/48/64/128/256）+ `icon.icns` + `public/icon.svg`（内嵌 PNG 的 data URI）
- UI 品牌标记 `BrandMark` 已改为 `<img src="/avatar/app-icon.png">`，保证与 exe 图标完全一致

### D. 工程化
- **测试**：Python 46 项（`lx-downloader/tests/test_engine.py`）、Rust 27 项（`lib.rs` 内 `mod tests`）
- **CI**：`.github/workflows/ci.yml`，三个 job（引擎 / 前端 / 后端），后端含 `cargo clippy -D warnings`
- **文档**：`CHANGELOG.md`（新建）、`README.md`（作者与反馈、功能列表）、`BUILD.md`（引擎哈希机制、测试命令）、`lx-downloader/README.md`（新参数表）

---

## 三、验证状态（最后一次实测结果）

| 检查 | 结果 | 备注 |
|------|------|------|
| `python -m unittest discover -s tests` | **46 passed** | 刚复跑确认 |
| `cargo test` | **27 passed** | 21:0x 时确认 |
| `cargo clippy --all-targets -- -D warnings` | **0 warning** | `start_download` / `download_songs` 已加 `#[allow(clippy::too_many_arguments)]` |
| `npx tsc --noEmit` | **0 error** | 21:5x 前确认 |
| `npm run build` | 成功 | 但 **21:40 那次之后未再构建**，不含最新 BrandMark 改动 |
| `tauri build` | 成功（21:41） | **不含新图标/头像** |
| 搜索实测 | 正常 | 「周杰伦 晴天」「五月天 倔强」「邓紫棋 泡沫」均有结果 |
| 歌单展开实测 | 227 首 | love 歌单 |
| 单曲下载实测 | 成功 | `asmi - JUNK AND PONG`，`[成功] … [320k]` |
| 优雅停止实测 | 成功 | 日志出现「正在等待在途任务收尾」「下载已停止」 |
| 引擎自动升级实测 | 成功 | 哈希多次变化，旧目录清理干净 |

---

## 四、当前工作区状态

### 已修改（M）
```
.gitignore  BUILD.md  README.md
lx-downloader/README.md
lx-downloader/lx_music_downloader.py
lx-downloader/lx_url_server.js
lx-tauri/index.html
lx-tauri/package.json
lx-tauri/src-tauri/Cargo.lock  Cargo.toml
lx-tauri/src-tauri/tauri.conf.json
lx-tauri/src-tauri/src/lib.rs
lx-tauri/src-tauri/icons/*（14 个 png + ico + icns）
lx-tauri/src/App.tsx  App.css
```

### 已删除（D）
```
lx-tauri/public/tauri.svg
lx-tauri/public/vite.svg
```

### 新增（??）
```
.github/workflows/ci.yml
CHANGELOG.md
lx-downloader/tests/test_engine.py
lx-tauri/public/avatar/{app-icon.png, author-256.jpg, author-460.jpg}
lx-tauri/public/icon.svg
lx-tauri/scripts/gen_icons.py
lx-tauri/scripts/_source_icon.webp      ← 图标母版，490KB
lx-tauri/src/icons.tsx
```

### 已清理的临时文件
已删除：`_avatar_try.png`、`lx-tauri/_master.png`、各处 `__pycache__`、
`lx-downloader/_t_out/`、`lx-downloader/_t_songs.json`、`_shot*.png`、`_capture.ps1` 等。
（`scripts/_source_icon.webp` **保留**，它是图标母版，需要入库）

---

## 五、恢复工作：下一步该做什么

### 第 1 步：重新构建（必须，否则 EXE 无新图标）

```powershell
cd "C:\Users\God Know\Documents\code\lx-music-download\lx-tauri"
npx tsc --noEmit                 # 先确认类型
npm run tauri build -- --no-bundle
```

然后（**先关掉正在运行的应用**，否则复制会报「文件正由另一进程使用」）：

```powershell
Get-Process -Name "落雪音乐下载器" -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2
Copy-Item "lx-tauri/src-tauri/target/release/lx-tauri.exe" "落雪音乐下载器.exe" -Force
```

### 第 2 步：验证新 EXE

```powershell
# 启动并确认不闪退
$p = Start-Process -FilePath ".\落雪音乐下载器.exe" -PassThru
Start-Sleep -Seconds 20
"running: $(-not $p.HasExited)"; (Get-Process -Id $p.Id).MainWindowTitle
```

```powershell
# 确认 exe 内嵌图标已是新设计稿（提取 32px 看符头/符干/符尾/雪花）
Add-Type -AssemblyName System.Drawing
$ico = [System.Drawing.Icon]::ExtractAssociatedIcon("$PWD\落雪音乐下载器.exe")
$ico.ToBitmap().Save("$PWD\_check.png")
```

### 第 3 步：提交与打 tag（建议拆成 5 个 commit）

```powershell
git add lx-downloader/lx_music_downloader.py lx-downloader/lx_url_server.js lx-downloader/tests .github CHANGELOG.md
git commit -m "fix: 修复音质档位/优雅停止/配置自愈/引擎升级，补齐测试与 CI"

git add lx-tauri/src-tauri/src/lib.rs lx-tauri/src-tauri/Cargo.toml lx-tauri/src-tauri/Cargo.lock
git commit -m "feat: 新增单曲搜索、歌单展开、落雪音源导入、关于页后端命令"

git add lx-tauri/src/App.tsx lx-tauri/src/App.css lx-tauri/src/icons.tsx lx-tauri/index.html lx-tauri/public
git commit -m "feat: 新增搜索单曲/关于页，歌单可展开逐首下载"

git add lx-tauri/src-tauri/icons lx-tauri/scripts
git commit -m "chore: 更换应用图标（八分音符 + 雪花）"

git add lx-tauri/package.json lx-tauri/src-tauri/tauri.conf.json README.md BUILD.md lx-downloader/README.md .gitignore
git commit -m "chore: 版本号升至 0.2.0，更新文档"

git tag v0.2.0
git push origin main --tags
```

---

## 六、已知问题与待办

### 待办（按优先级）

1. **[必须] 重新构建 EXE** —— 当前 EXE 是旧图标版本（见第一节）
2. **提交代码** —— 全部改动未入库，`git status` 有 30+ 项
3. **`--min-quality` 默认值** —— 目前默认 `320k`，意味着默认不会降到 192k/128k。
   这是沿用的旧设计（「绝不低于 320k」）。若希望默认允许降到 128k，需改
   `lx_music_downloader.py` 里 `--min-quality` 的 `default`，以及 `App.tsx` 的 `useState("320k")`
4. **搜索源稳定性** —— 酷我老接口 `search.kuwo.cn/r.s` 是无鉴权的老接口，随时可能下线。
   若搜索页某天返回空，优先检查这里。QQ 接口当前已 500，oiapi 是备选。
5. **歌单页日志** —— `loadPlaylists()` 不再清空日志（已改），但切换 Tab 时各页共用同一个 `logs` 数组，
   观感上像「日志串页」，如需要可按 Tab 分开存储

### 已知限制（不是 bug，是设计取舍）

- **搜索结果的「伴奏/翻唱」**：相关度排序会把它们压到后面并降权，但**不会过滤**——
  因为用户可能确实想下伴奏。真正的拦截在下载阶段的「版本词校验」
- **单曲下载没有替代源**：`run_download(db_path=None)` 时跳过 `get_alternative_sources`，
  因为单曲没有数据库上下文。失败后仍会走「按名搜索」兜底
- **128k/192k 档位**：引擎按请求档位验收（请求 128k 只要 ≥128k），但**音源脚本本身**
  可能拿不到真正的低码率链接，此时会拿到高码率，属于正常（更高音质永远通过）

---

## 七、关键代码位置索引

### 引擎（Python）
| 功能 | 位置 |
|------|------|
| 音质门槛表 | `lx_music_downloader.py` → `QUALITY_MIN_BITRATE` |
| 音质校验 | `verify_quality(path, quality)` |
| 降级序列 | `lower_quality_options(quality, min_quality)` |
| 下载管线 | `run_download(args, songs, db_path, title)` |
| 四重校验 | `download_with_candidates(..., requested_quality=)` |
| 搜索 | `run_search(args)` |
| 歌曲文件解析 | `load_song_file(path)` |
| 优雅停止 | `watch_stop_file(path)` + `DOWNLOAD_STOP_EVENT` |
| 配置自愈 | `load_config()` / `_dir_is_usable()` / `_looks_mojibake()` |

### 桥接（Node）
| 功能 | 位置 |
|------|------|
| 搜索命令入口 | `lx_url_server.js` → `rl.on('line')` 里 `msg.cmd === 'searchSongs'` |
| 搜索主逻辑 | `searchSongs(keyword, limit)` |
| 酷我直连 | `kuwoSearch(kw, limit)` |
| 网易云直连 | `neteaseSearch(kw)` |
| 相关度排序 | `searchSongs` 内 `scoreOf(s)` |
| 取链接 | `getMusicUrls(source, musicInfo, qualities)` |
| 音质档位比较 | `QUALITY_RANK` / `qualityRank(q)` |

### 后端（Rust）
| 功能 | 位置（`lx-tauri/src-tauri/src/lib.rs`） |
|------|------|
| 引擎释放/升级 | `ensure_engine()` / `engine_version()` |
| 优雅停止 | `stop_task()` / `stop_signal_path()` |
| 搜索 | `search_songs()` (L880) |
| 列歌 | `list_songs()` (L906) |
| 单曲下载 | `download_songs()` (L941) |
| JSON 解析辅助 | `run_engine_json()` (L1004) |
| 落雪音源探测 | `find_lx_desktop_api()` (L667) |
| 落雪音源导入 | `import_lx_desktop_sources()` (L693) |
| gz 解码 | `decode_lx_script()` |
| 文件名清理 | `sanitize_file_stem()` |

### 前端（React）
| 功能 | 位置（`lx-tauri/src/App.tsx`） |
|------|------|
| Tab 定义 | `type Tab`（7 个：playlists/search/download/export/sources/settings/about） |
| 作者常量 | `AUTHOR` |
| 平台名映射 | `SOURCE_LABELS` / `sourceLabel()` |
| 时长格式化 | `formatInterval()` |
| 搜索 | `doSearch()` |
| 歌单展开 | `toggleSongs()` |
| 单曲/批量下载 | `downloadSongs(list, label)` |
| 落雪音源导入 | `importFromLxDesktop()` / `pickLxApiFile()` |
| 关于页 | `tab === "about"` 分支 |
| 品牌标记 | `src/icons.tsx` → `BrandMark` |

---

## 八、环境信息

| 组件 | 版本 |
|------|------|
| Python | 3.13.14 |
| Node.js | 22.19.0 |
| npm | 10.9.3 |
| rustc | 1.98.0 |
| PyInstaller | 6.22.0 |
| mutagen | 1.48.1 |
| Pillow | 已安装（图标脚本依赖） |

### 关键路径
- 落雪数据库：`%APPDATA%\lx-music-desktop\LxDatas\lx.data.db`
- 落雪音源：`%APPDATA%\lx-music-desktop\LxDatas\user_api.json`
- 本程序音源目录：`%APPDATA%\lx-downloader-sources\`
- 引擎释放目录：`%LOCALAPPDATA%\落雪下载\engine\`
- 引擎配置：`%APPDATA%\lx-downloader-config.json`
- 默认下载目录（冻结版）：`~/Music/落雪音乐下载`
- 默认下载目录（开发版）：`lx-downloader/downloads`

---

## 九、坑与注意事项

1. **PowerShell 控制台会把 UTF-8 中文显示成乱码**（如 `钀介洩闊充箰`）。
   判断路径是否真的乱码，**必须做字节级检查**：
   ```powershell
   python -c "import json,os;d=json.loads(open(os.path.join(os.environ['APPDATA'],'lx-downloader-config.json'),'rb').read().decode('utf-8'));print(d['dir'])"
   ```
   不要相信 `Get-Content` 的显示结果。

2. **不要用 PowerShell 的 `Set-Content` 改 `package.json`**。
   我踩过：它加了 BOM 且把文件写空，导致 Vite 把 package.json 当 PostCSS 配置解析并报
   `Unexpected token ''`。**改文本文件一律用编辑工具**，不要用 shell 重定向。

3. **PowerShell 传中文命令行参数会损坏**。
   测试引擎的 `--search "周杰伦 晴天"` 请用 Python 的 `subprocess` 调用，
   直接在 PowerShell 里写中文参数会得到空结果（不是代码 bug）。

4. **`engine.zip` 被 `.gitignore` 忽略**，是构建产物。
   改 Python 引擎/Node 桥接/音源后**必须重新打包**：
   ```powershell
   cd lx-downloader
   python -m PyInstaller --noconfirm --clean lx_music_downloader.spec
   # 然后把 dist/lx_music_downloader 内容打成 zip 覆盖 lx-tauri/src-tauri/engine/engine.zip
   ```
   打包前需确认 `lx-downloader/_pack/node/node.exe` 存在（从 `C:\Program Files\nodejs\node.exe` 复制）。
   只改前端/Rust 时**不需要**重打引擎。

5. **复制 EXE 前必须关掉正在运行的应用**，否则报「文件正由另一进程使用」。

6. **Tauri 命令参数多不会被 clippy 放过**，需要显式
   `#[allow(clippy::too_many_arguments)]`（`start_download`、`download_songs` 已加）。

---

## 十、参考：本轮之前的基线

`git HEAD = ca19dfd`（v0.1.1）时的状态：
- 无测试、无 CI
- 5 个音质档位里 128k/192k 实际不可用
- 停止是硬杀
- 引擎用 zip 长度做版本标记
- 图标是 Tauri 默认蓝黄 logo
- 只有 5 个页面（歌单/下载/导出/音源/设置）
- 没有搜索、没有落雪音源导入、没有关于页

对比现在，本轮新增/修复的内容即上文第二、三节所列。
