# 落雪音乐下载器 (lx-music-downloader)

> 自动读取落雪音乐（lx-music-desktop）本地歌单，多音源批量无损下载的 Windows 音乐下载工具（Tauri 图形界面 + Python 引擎）。

![platform](https://img.shields.io/badge/platform-Windows-0078d6) ![license](https://img.shields.io/badge/license-GPL--3.0-blue)

---

## ⚠️ 法律与版权声明（请先阅读）

本项目是一个**音乐下载工具**，本身不包含任何音乐内容，但会借助第三方「音源脚本」从各大音乐平台获取播放链接并下载音频文件。公开托管本仓库存在如下风险，请自行评估后再使用/传播：

- **版权风险**：下载受版权保护的音乐（尤其是用于再分发或商业用途）在绝大多数法域均属侵权。个人学习、自用测试尚在灰色地带，但公开传播、打包分享将显著提高被追责风险。
- **音源由用户自备**：本仓库**不捆绑、不分发**任何第三方音源脚本。音源由使用者在「音源」页自行导入（`%APPDATA%\lx-downloader-sources\`），因此本仓库不涉及对第三方音源脚本的再分发。但导入的音源若用于绕过平台访问控制，相关责任由使用者自行承担。
- **技术措施规避**：部分音源脚本绕过平台访问控制，可能触及《著作权法》关于技术措施的规定或平台 ToS。
- 本项目仅用于**个人学习与研究**。请勿将下载内容用于商业用途或传播；建议试听后删除并支持正版。

> 若介意上述风险，请将仓库保持 **private** 使用。

## 功能特性

- **一键下载**：自动探测本机落雪音乐数据库，列出歌单（默认列表、我的收藏、自定义）
- **多平台支持**：酷我(kw)、网易云(wy)、腾讯(tx)、酷狗(kg)、咪咕(mg)
- **无损优先**：默认目标音质 FLAC / FLAC 24bit，绝不低于 320k
- **自定义音源**：在「音源」页导入你自己的音源脚本，保存在 `%APPDATA%\lx-downloader-sources\`
- **四重校验**：时长 / 版本词 / 歌手标签 / 音质（按文件内容探测，非扩展名）
- **失败重试**：全部失败歌曲二次重试，仍失败写入 `failed.txt`，可重跑补下
- **免安装 EXE**：内置 Python 引擎 + Node.js + ffmpeg，双击即用

## 快速开始

### 方式一：图形界面（推荐）

**直接下载 EXE**：从 [GitHub Releases](https://github.com/MyGodKnow/lx-music-downloader/releases) 下载最新 `lx-music-downloader-*.exe`，双击即用（首次运行自动释放内置引擎，免装 Python / Node）。

**源码运行**（开发者）：

```bash
cd lx-tauri
npm install
npm run tauri dev        # 开发模式运行
```

使用前先在「音源」页**导入你自己的音源脚本**（.js / .json / user_api.json），再点「刷新歌单」自动探测落雪数据库；若未安装落雪音乐，可手动导入 `.lxmc` / `.json` 歌单文件。

### 方式二：命令行引擎

```bash
cd lx-downloader
python lx_music_downloader.py --list                  # 列出歌单
python lx_music_downloader.py --playlist love --quality flac --dir D:/Music --workers 4
```

命令行同样默认从 `%APPDATA%\lx-downloader-sources\` 加载音源；未导入音源时会提示「请先导入音源」。完整参数与说明见 [lx-downloader/README.md](lx-downloader/README.md)。

## 音源管理（用户自备）

- 本仓库**不捆绑任何第三方音源**。请在 GUI「音源」页导入你自己的音源脚本（.js / .json / user_api.json），导入后保存在 `%APPDATA%\lx-downloader-sources\`，可随时增删，不随程序更新丢失
- 也可直接把音源文件放进该目录，或把落雪音乐桌面版导出的 `user_api.json` 放进去
- 引擎默认从该目录加载全部音源；未导入任何音源时，下载会提示「请先导入音源」并中止

## 免责声明

本项目仅供个人学习与研究使用。使用者应尊重音乐版权，下载内容请勿用于商业用途或传播，并建议及时删除测试内容、支持正版。因使用本项目产生的任何法律问题由使用者自行承担。
