# 落雪音乐下载器 (lx-music-downloader)

> 自动读取落雪音乐（lx-music-desktop）本地歌单，多音源批量无损下载 + 图形界面（Tauri）的 Windows 音乐下载工具。

![platform](https://img.shields.io/badge/platform-Windows-0078d6) ![license](https://img.shields.io/badge/license-GPL--3.0-blue)

---

## ⚠️ 法律与版权声明（请先阅读）

本项目是一个**音乐下载工具**，本身不包含任何音乐内容，但会借助第三方「音源脚本」从各大音乐平台获取播放链接并下载音频文件。公开托管本仓库存在如下风险，请自行评估后再使用/传播：

- **版权风险**：下载受版权保护的音乐（尤其是用于再分发或商业用途）在绝大多数法域均属侵权。个人学习、自用测试尚在灰色地带，但公开传播、打包分享将显著提高被追责风险。
- **第三方音源脚本**：`lx-downloader/sources/*.js` 为社区维护的第三方脚本，其著作权归各自作者所有，本仓库并未获得授权再分发。公开仓库包含它们，可能涉及对原作者著作权的侵害，也可能因违反平台条款而被投诉下架（DMCA）。
- **技术措施规避**：部分音源脚本绕过平台访问控制，可能触及《著作权法》关于技术措施的规定或平台 ToS。
- 本项目仅用于**个人学习与研究**。请勿将下载内容用于商业用途或传播；建议试听后删除并支持正版。

> 若介意上述风险，请将仓库保持 **private** 或移除 `sources/` 目录后自行使用。

---

## 功能特性

- **一键下载**：自动探测本机落雪音乐数据库（官方版/便携版/多种 fork），列出歌单（默认列表、我的收藏、自定义）
- **多平台支持**：酷我(kw)、网易云(wy)、腾讯(tx)、酷狗(kg)、咪咕(mg)
- **无损优先**：默认目标音质 FLAC/FLAC 24bit，最高不低于 320k，绝不降级到 128k 以下
- **多级兜底**：原源直查 → 替代平台直查 → 按「歌手+歌名」全网搜索
- **四重校验**（下载防错核心）：
  1. 时长校验（±15s，防下错歌）
  2. 版本拦截（伴奏/翻唱/卡拉OK/remix/纯音乐 等关键词）
  3. 歌手校验（artist 标签与目标一致，防翻唱冒充原唱）
  4. 音质校验（**按文件内容探测**，不是看扩展名，拦截“假 FLAC”）
- **失败重试与清单**：全部失败的歌曲二次重试，仍失败写入 `failed.txt`，可稍后重跑自动补下
- **MP3 转换**：可将无损/高码率文件并行转换为 320k MP3，集中到 `mp3/` 子目录
- **图形界面（GUI）**：Tauri 2 + React 暗色主题，歌单 / 下载 / 转换 / 设置四个页面，实时日志与进度
- **单文件 EXE 分发**：内置 Python 引擎 + Node.js + 音源 + ffmpeg，免安装运行

---

## 快速开始

### 方式一：图形界面（推荐）

```bash
cd lx-tauri
npm install
npm run tauri dev        # 开发模式运行
```

启动后点「刷新歌单」自动探测落雪数据库；若未安装落雪音乐，可手动导入 `.lxmc` / `.json` 歌单文件。

### 方式二：命令行引擎

```bash
cd lx-downloader
python lx_music_downloader.py --list                  # 列出歌单
python lx_music_downloader.py --playlist love --quality flac --dir D:/Music --workers 4
```

完整参数与说明见 [lx-downloader/README.md](lx-downloader/README.md)。

---

## 项目结构

```
.
├── lx-downloader/                  # Python 下载引擎（核心）
│   ├── lx_music_downloader.py      # 主程序：歌单解析→取链接→四重校验→并发下载
│   ├── lx_url_server.js            # Node 桥接服务：加载音源、取播放链接、按名搜索
│   ├── sources/                    # 启用中的音源脚本（实测有效 5 个）
│   │   └── disabled/               # 失效音源（保留参考，不加载）
│   ├── _probe_sources_v2.py        # 音源健康检查工具（逐源跑 searchByName 全流程）
│   ├── scan_sources.py             # 音源实测工具（歌单抽样逐源验证）
│   ├── analyze_quality.py          # 下载文件质量分析工具
│   └── README.md                   # 引擎详细文档
├── lx-tauri/                       # Tauri 2 图形界面（Rust 后端 + React 前端）
│   ├── src/                        # React 前端（暗色主题，歌单/下载/设置页）
│   └── src-tauri/
│       ├── src/lib.rs              # 命令：list_playlists / start_download / start_convert / stop_task
│       ├── engine/                 # 内置下载引擎压缩包（构建产物，不入库，见 BUILD.md）
│       └── tauri.conf.json         # 应用配置
├── BUILD.md                        # 打包 / 构建 / 引擎更新流程
├── LICENSE                         # GPL-3.0
└── README.md
```

---

## 质量保证（硬性约束）

- 所有下载文件必须达到 **无损（FLAC/ALAC/APE/WAV）** 或 **≥320k** 标准
- 音质以**文件内容**（mutagen 探测）而非扩展名为准，防止假 FLAC
- 标题含 `伴奏/カラオケ/karaoke/Instrumental/off vocal/翻唱/cover/remix/纯音乐` 一律拒绝
- artist 标签必须与目标歌手一致
- 低质量文件会被移入 `low_quality/` 子目录，失败记录写入 `failed.txt`

---

## 音源维护

- 新音源放入 `lx-downloader/sources/` 即被自动加载
- 拿不到有效链接的源会被冷却（默认 10 分钟），避免拖慢下载
- 失效音源移入 `sources/disabled/` 停用
- 健康检查：`python _probe_sources_v2.py` / `python scan_sources.py`

---

## 构建与打包

完整的 EXE 打包、内置引擎重建、版本更新流程见 **[BUILD.md](BUILD.md)**。

---

## 技术栈

| 层 | 技术 |
|----|------|
| 下载引擎 | Python 3.13（标准库 + mutagen）+ Node.js 22 音源桥接 |
| 图形界面 | Tauri 2（Rust）+ React 19 + Vite + TypeScript |
| 音质校验 | mutagen（按文件内容探测码率/格式） |
| 打包分发 | PyInstaller（onedir）+ include_bytes 内嵌引擎 + 单文件 EXE |

---

## 免责声明

本项目仅供个人学习与研究使用。使用者应尊重音乐版权，下载内容请勿用于商业用途或传播，并建议及时删除测试内容、支持正版。因使用本项目产生的任何法律问题由使用者自行承担。
