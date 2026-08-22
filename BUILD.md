# 构建与打包指南

本仓库采用「Python 引擎 + Tauri 壳」架构，最终交付为单文件 EXE。以下为完整构建流程，供维护者复现。

## 环境要求

| 组件 | 版本 | 说明 |
|------|------|------|
| Python | 3.13 | 引擎本体，需能运行 `lx_music_downloader.py` |
| Node.js | 22+ | 音源桥接运行环境，需含 `node.exe` |
| ffmpeg | 任意新版本 | 用于格式转换/质量探测，需含 `ffmpeg.exe` 及其 DLL |
| Rust | stable | Tauri 后端 |
| VS 2022 Build Tools | - | Rust 的 MSVC 链接器（含 WebView2） |

## 目录约定

- `lx-downloader/`：引擎源码，可独立运行（`python lx_music_downloader.py`）
- `lx-tauri/`：Tauri 壳，前端 + Rust 后端
- `lx-tauri/src-tauri/engine/engine.zip`：**内置引擎压缩包**（构建产物，不入库）

## 单文件 EXE 构建流程

> 引擎 = PyInstaller 冻结的 `lx_music_downloader`（内含 Python 运行时、Node、音源脚本、ffmpeg），整体打 zip 后由 Rust `include_bytes!` 编译期内嵌进 EXE；首次运行释放到 `%LOCALAPPDATA%\落雪下载\engine\` 并执行，免安装 Python/Node。

### 1. 重建下载引擎（engine.zip）

```bash
cd lx-downloader

# 1) 暂存运行依赖到 _pack（PyInstaller 打包后需要）
#    node/ffmpeg 及其 DLL 放入 _pack/（按需保留，打包后可删除）
#    目录结构参考：
#   _pack/
#   ├── node/node.exe
#   ├── ffmpeg/ffmpeg.exe
#   └── ... (音源相关)

# 2) 用 PyInstaller 冻结引擎（onedir，含 mutagen 与音源桥接）
pyinstaller --onedir --collect-all mutagen ^
  --add-data "lx_url_server.js;." ^
  --add-data "sources;sources" ^
  --add-data "_pack;." ^
  lx_music_downloader.py

# 3) 将 dist/lx_music_downloader 整体打 zip（zip 内文件在根级，不含外层目录）
#    覆盖写入 lx-tauri/src-tauri/engine/engine.zip
```

> 注意：`engine.zip` 内容根级结构需与 `lib.rs::ensure_engine()` 的预期一致（即解压后直接得到 `lx_music_downloader.exe`、`lx_url_server.js`、`sources/`、`node/`、`ffmpeg/`）。

### 2. 编译 Tauri 发布版

```bash
cd lx-tauri
npm install
npm run tauri build -- --no-bundle     # 产出 target/release/lx-tauri.exe
```

### 3. 产出最终 EXE

```bash
# 复制到项目根目录，改名
copy target\release\lx-tauri.exe "..\落雪音乐下载器.exe"
```

桌面快捷方式指向 `落雪音乐下载器.exe` 即可。

## 引擎版本更新机制

- `lib.rs::ensure_engine()` 以 **内嵌 zip 的长度** 作为版本标记写入 `%LOCALAPPDATA%\落雪下载\engine\.engine_size`
- 每次重建引擎后 zip 长度变化，用户下次启动会自动重新释放新引擎
- 因此：**只改前端/后端 Rust 代码，无需重打 engine.zip**；只改 Python 引擎/音源/Node/ffmpeg 时才需要重建引擎

## 常见问题

**Q: 编译报 `cannot find ../engine/engine.zip`？**
engine.zip 是构建产物、已 gitignore。请先按上述「1. 重建下载引擎」生成该文件。

**Q: 换了音源脚本怎么生效？**
把新音源放进 `lx-downloader/sources/` 后**重新执行第 1 步打包**（引擎 zip 里才包含新音源），或开发模式下用 `--api-dir` 直接指定音源目录。

**Q: 只想改 GUI 文案/界面？**
只改 `lx-tauri/src/` 下 React 代码，然后 `npm run tauri dev` 预览；发布时只需第 2 步（无需重建引擎 zip）。
