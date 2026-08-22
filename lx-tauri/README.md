# 落雪音乐下载器 - GUI (lx-tauri)

落雪音乐下载器的桌面图形界面，基于 **Tauri 2 + React 19 + Vite + TypeScript**。

## 功能页面

| 页面 | 说明 |
|------|------|
| 歌单 | 自动探测落雪音乐数据库，列出歌单并选择下载 |
| 下载 | 音质选择（FLAC 24bit / FLAC / 320k / 192k / 128k）、并发数、下载目录、实时日志与进度 |
| 音源 | 导入/删除用户自己的音源脚本（.js / .json），保存在 `%APPDATA%\lx-downloader-sources\` |
| 设置 | 数据库路径、Python 路径（开发模式）、音质兜底策略 |

## 开发

```bash
npm install
npm run tauri dev     # 启动开发模式（Vite :1420 + Tauri）
```

## 构建发布

```bash
npm run tauri build -- --no-bundle   # 产物在 src-tauri/target/release/lx-tauri.exe
```

## 后端命令（Rust）

见 [src-tauri/src/lib.rs](src-tauri/src/lib.rs)：

- `list_playlists`：列出歌单
- `start_download` / `stop_task`：启动下载 / 停止当前任务（连带清理 python/node/ffmpeg 子进程树）
- 音源管理：`get_sources_dir`（音源目录）、`list_sources`（已导入列表）、`import_source`（导入）、`remove_source`（删除）

## 引擎集成

GUI 本身不含下载逻辑，运行时调用 `lx-downloader` 引擎：

1. 若用户在设置中指定了 Python 路径 → 开发模式：`python lx_music_downloader.py`
2. 否则优先使用**内置引擎**（编译期内嵌的 `engine/engine.zip`，首启释放到 `%LOCALAPPDATA%\落雪下载\engine\`）
3. 兜底：系统 `python` + 脚本（仅开发环境可用）

完整打包与引擎重建流程见根目录 [BUILD.md](../BUILD.md)。
