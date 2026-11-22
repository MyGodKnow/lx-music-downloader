# 构建与打包指南

本仓库采用「Python 引擎 + Tauri 壳」架构，最终交付为单文件 EXE。以下为完整构建流程，供维护者复现。

## 环境要求

| 组件 | 版本 | 说明 |
|------|------|------|
| Python | 3.13 | 引擎本体，需能运行 `lx_music_downloader.py` |
| Node.js | 22+ | 音源桥接运行环境，需含 `node.exe` |
| Rust | stable | Tauri 后端 |
| VS 2022 Build Tools | - | Rust 的 MSVC 链接器（含 WebView2） |

## 目录约定

- `lx-downloader/`：引擎源码，可独立运行（`python lx_music_downloader.py`）
- `lx-tauri/`：Tauri 壳，前端 + Rust 后端
- `lx-tauri/src-tauri/engine/engine.zip`：**内置引擎压缩包**（构建产物，不入库）

## 单文件 EXE 构建流程

> 引擎 = PyInstaller 冻结的 `lx_music_downloader`（内含 Python 运行时、Node、音源桥接），整体打 zip 后由 Rust `include_bytes!` 编译期内嵌进 EXE；首次运行释放到 `%LOCALAPPDATA%\落雪下载\engine\` 并执行，免安装 Python/Node。

### 1. 重建下载引擎（engine.zip）

```bash
cd lx-downloader

# 1) 暂存运行依赖到 _pack（PyInstaller 打包后需要）
#    node.exe 放入 _pack/（按需保留，打包后可删除）
#    目录结构参考：
#   _pack/
#   └── node/node.exe

# 2) 用 PyInstaller 冻结引擎（onedir，含 mutagen 与音源桥接）
#    spec 已固化上述参数，直接跑 spec 即可，避免手写参数漏项
python -m PyInstaller --noconfirm --clean lx_music_downloader.spec

# 3) 将 dist/lx_music_downloader 整体打 zip（zip 内文件在根级，不含外层目录）
#    覆盖写入 lx-tauri/src-tauri/engine/engine.zip
```

> 注意：
> - `engine.zip` 内容根级结构需与 `lib.rs::ensure_engine()` 的预期一致（即解压后直接得到 `lx_music_downloader.exe`、`_internal/lx_url_server.js`、`_internal/sources/`、`_internal/node/`）。
> - **第三方音源不再随引擎捆绑**：引擎运行时默认从 `%APPDATA%\lx-downloader-sources\` 读取用户导入的音源。`sources/` 只是占位目录（含 `.gitkeep` 与 README），不包含任何音源脚本。
> - 已移除 MP3 转换功能，引擎不再打包 ffmpeg。
> - 打包前先跑测试：`python -m unittest discover -s tests -v`。

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

- `lib.rs::ensure_engine()` 以**内嵌 zip 的 SHA-256 内容哈希**作为版本标记，写入 `%LOCALAPPDATA%\落雪下载\engine\.engine_hash`
- 任何字节变化（哪怕长度不变）都会触发重新释放；早期用「zip 长度」判断，内容微调时可能长度不变而卡在旧引擎
- 升级采用「解压到 `engine.staging` → 旧目录改名为 `engine.old` → 新目录改名到位」的目录交换：
  旧引擎文件被运行中的进程占用时也能完成升级，不会因删不掉旧目录而失败
- 因此：**只改前端/后端 Rust 代码，无需重打 engine.zip**；只改 Python 引擎/音源/Node 时才需要重建引擎

## 测试

```bash
cd lx-downloader && python -m unittest discover -s tests -v   # 引擎纯逻辑 33 项
cd lx-tauri && npx tsc --noEmit                                # 前端类型检查
cd lx-tauri/src-tauri && cargo test                            # 后端 48 项
cd lx-tauri/src-tauri && cargo clippy --all-targets -- -D warnings
```

CI（`.github/workflows/ci.yml`）在 push / PR 时跑上述全部检查。

## 运行数据目录（AppData 不可写时的回退）

`lib.rs::data_root()` 按顺序**实测可写性**（真的建一个 `.write-probe` 探针文件再删掉），
选第一个能写的作为数据根目录：

1. `%LOCALAPPDATA%\落雪下载\`（默认，首选）
2. `<EXE 所在目录>\落雪下载数据\`（便携版/绿色版友好，也是 AppData 被锁死时的第一备选）
3. `%TEMP%\lx-downloader\`
4. `<当前工作目录>\lx-downloader-data\`

以下三项都跟随这个根目录，不是硬编码在 AppData：

| 内容 | 路径 |
|---|---|
| 启动日志 | `<root>\startup.log` |
| 下载引擎副本 | `<root>\engine\`（含 `.engine_hash`） |
| WebView2 profile | 默认 `<AppData>\com.lxmusic.downloader\EBWebView`；不可写时改用 `<root>\webview2\` |

这样设计的原因：这三样东西一旦写不进去，程序会以最难排查的方式失败——
日志写不出（崩溃无从诊断）、引擎解压失败、WebView2 起不来（表现为「闪退」）。
**不要把这里的路径改回硬编码**，除非你确认所有目标机器都保证 AppData 可写。

> 注意：回退目录属于本机运行状态，已加入 `.gitignore`，不要提交。

## 常见问题

**Q: 编译报 `cannot find ../engine/engine.zip`？**
engine.zip 是构建产物、已 gitignore。请先按上述「1. 重建下载引擎」生成该文件。

**Q: 换了音源脚本怎么生效？**
把新音源放进 `lx-downloader/sources/` 后**重新执行第 1 步打包**（引擎 zip 里才包含新音源），或开发模式下用 `--api-dir` 直接指定音源目录。

**Q: 只想改 GUI 文案/界面？**
只改 `lx-tauri/src/` 下 React 代码，然后 `npm run tauri dev` 预览；发布时只需第 2 步（无需重建引擎 zip）。

**Q: 直接用 `cargo build --release` 出的 exe，双击后显示「localhost 拒绝连接」？**
这是正常的：`cargo build` **不会内嵌前端资源**，exe 仍按 `tauri.conf.json` 的 `devUrl`
去连 Vite（`http://localhost:1420`）。正式包必须用：

```bash
cd lx-tauri && npx tauri build --no-bundle
```

它才会先跑 `vite build` 把 `dist/` 打进二进制。两者产物路径相同，容易混淆——
**看到 `localhost 拒绝连接` 就说明你拿到的是 dev 产物**，不要发布它。

**Q: 双击后窗口闪一下就没了（进程还在任务管理器里）？**
这是 WebView2 初始化失败的典型表现：`%LOCALAPPDATA%` 下的 profile 建不出来
（受限账户 / 组策略重定向 / 安全软件拦截未签名程序写入），
`CreateCoreWebView2Controller` 失败后 wry 销毁窗口，但 Tauri 事件循环不会因此退出。

当前版本已针对此做了三层防护，排查时按顺序看：
1. **`%LOCALAPPDATA%\落雪下载\startup.log`** —— 记录 exe 路径、WebView2 运行时版本、
   每次尝试的数据目录与错误详情。**先看这个文件**。
2. 数据目录自适应：AppData 不可写会自动回退到 EXE 同级 / TEMP，日志里会写明实际用的哪个。
3. 看门狗：若窗口仍在启动后消失，会弹系统框说明原因并退出，不再残留隐形进程。

若日志显示 `open=ERR(permission denied)`，检查安全软件是否拦截了本程序，
或该目录的 ACL 是否异常（见下方「运行数据目录」）。
