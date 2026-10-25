# 落雪音乐下载器 (lx-music-downloader)

> 自动读取落雪音乐（lx-music-desktop）本地歌单，多音源批量无损下载的 Windows 音乐下载工具（Tauri 图形界面 + Python 引擎）。

[![release](https://img.shields.io/github/v/release/MyGodKnow/lx-music-downloader?label=最新版本&color=2ea44f)](https://github.com/MyGodKnow/lx-music-downloader/releases/latest)
[![downloads](https://img.shields.io/github/downloads/MyGodKnow/lx-music-downloader/total?label=下载量&color=0078d6)](https://github.com/MyGodKnow/lx-music-downloader/releases)
[![stars](https://img.shields.io/github/stars/MyGodKnow/lx-music-downloader?label=Stars&color=e3b341)](https://github.com/MyGodKnow/lx-music-downloader/stargazers)
[![platform](https://img.shields.io/badge/平台-Windows%2010%2F11-0078d6)](#系统要求)
[![license](https://img.shields.io/badge/许可证-GPL--3.0-blue)](LICENSE)

**落雪音乐歌单批量下载器** · 桌面 GUI · 多音源自动取链 · 无损音质 · 免安装单文件 EXE

支持从 [落雪音乐（lx-music-desktop）](https://github.com/lyswhut/lx-music-desktop) 读取歌单，
通过用户自备音源批量下载网易云 / 酷我 / QQ音乐 / 酷狗 / 咪咕的歌曲。

---

## 作者的话

这个工具最初只是给自己用的：落雪音乐的歌单攒了几百首，一首首手动导出太麻烦，
于是写了个脚本自动读数据库、自动找音源、自动下载。

后来发现身边不少人有同样需求，就加了图形界面打包成 EXE 放出来。
**它不下载、不分发任何音乐**，只是个「把你已经在听的歌保存到本地」的搬运工，
所有音源由使用者自己提供。

如果它帮你省下了下歌的时间，欢迎点个 ⭐ Star —— 不花钱，
但能让更多人看到它，也是我继续维护的动力。

有问题或建议，欢迎提 [Issue](https://github.com/MyGodKnow/lx-music-downloader/issues) 或发邮件，
我都会看。

## ⚠️ 法律与版权声明（请先阅读）

本项目是一个**音乐下载工具**，本身不包含任何音乐内容，但会借助第三方「音源脚本」从各大音乐平台获取播放链接并下载音频文件。公开托管本仓库存在如下风险，请自行评估后再使用/传播：

- **版权风险**：下载受版权保护的音乐（尤其是用于再分发或商业用途）在绝大多数法域均属侵权。个人学习、自用测试尚在灰色地带，但公开传播、打包分享将显著提高被追责风险。
- **音源由用户自备**：本仓库**不捆绑、不分发**任何第三方音源脚本。音源由使用者在「音源」页自行导入（`%APPDATA%\lx-downloader-sources\`），因此本仓库不涉及对第三方音源脚本的再分发。但导入的音源若用于绕过平台访问控制，相关责任由使用者自行承担。
- **技术措施规避**：部分音源脚本绕过平台访问控制，可能触及《著作权法》关于技术措施的规定或平台 ToS。
- 本项目仅用于**个人学习与研究**。请勿将下载内容用于商业用途或传播；建议试听后删除并支持正版。

> 若介意上述风险，请将仓库保持 **private** 使用。

## 功能特性

- **一键下载**：自动探测本机落雪音乐数据库，列出歌单（默认列表、我的收藏、自定义）
- **搜索单曲**：按「歌手 歌名」搜索全网，勾选后可批量下载，也可单曲直接下载
- **歌单展开**：点歌单卡片即可查看全部歌曲，支持逐首下载或整张下载
- **多平台支持**：酷我(kw)、网易云(wy)、腾讯(tx)、酷狗(kg)、咪咕(mg)
- **音质可选**：Hi-Res / 无损 / 320k / 192k / 128k，按请求档位验收；默认目标 FLAC，降级底线默认 320k
- **一键导入落雪音源**：直接读取落雪音乐 App 里已添加的音源（`user_api.json`），自动解码入库
- **自定义音源**：也可在「音源」页手动导入音源脚本，保存在 `%APPDATA%\lx-downloader-sources\`
- **四重校验**：时长 / 版本词 / 歌手标签 / 音质（按文件内容探测，非扩展名）
- **失败重试**：全部失败歌曲二次重试，仍失败写入 `failed.txt`，可重跑补下
- **免安装 EXE**：内置 Python 引擎 + Node.js，双击即用

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

## 系统要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 10 1803+ / Windows 11（64 位） |
| 磁盘空间 | 约 200 MB（程序 54 MB + 临时解压） |
| 网络 | 需要能访问音乐平台与 GitHub（检查更新） |
| 其他 | 无需安装 Python / Node.js，引擎已内置 |

> 更新包下载依赖系统自带 `curl.exe`（Windows 10 1803 起内置）。
> 若系统缺失，仍可手动到 [Releases](https://github.com/MyGodKnow/lx-music-downloader/releases) 下载。

## 音源管理（用户自备）

- 本仓库**不捆绑任何第三方音源**。请在 GUI「音源」页导入你自己的音源脚本（.js / .json / user_api.json），导入后保存在 `%APPDATA%\lx-downloader-sources\`，可随时增删，不随程序更新丢失
- **一键从落雪音乐导入**：「音源」页会探测 `%APPDATA%\lx-music-desktop\LxDatas\user_api.json`，把你在落雪 App 里已添加的音源（`gz_` 压缩格式）逐个解码成独立 `.js` 存入本程序音源目录；探测不到时可用「手动选择文件」
- 也可直接把音源文件放进该目录
- 引擎默认从该目录加载全部音源；未导入任何音源时，下载会提示「请先导入音源」并中止

## 作者与反馈

| 项目     | 信息                                                         |
| -------- | ------------------------------------------------------------ |
| 作者     | 我鬼知道（MyGodKnow）                                        |
| QQ       | 1973490065                                                   |
| 邮箱     | [114514woguizhidaoya@gmail.com](mailto:114514woguizhidaoya@gmail.com) |
| 问题反馈 | [GitHub Issues](https://github.com/MyGodKnow/lx-music-downloader/issues) |

遇到问题欢迎通过**邮箱**或 **GitHub Issues** 反馈。提交时请附上：操作步骤、日志区的报错内容、所用音源名称。

## 关键词

> 方便搜索，也是这个项目在做的事：

`落雪音乐下载器` · `落雪音乐歌单下载` · `lx-music 下载` · `lx-music-desktop 歌单导出` ·
`音乐歌单批量下载` · `无损音乐下载工具` · `FLAC 批量下载` · `网易云歌单下载` ·
`酷我音乐下载` · `QQ音乐歌单下载` · `酷狗音乐下载` · `多音源音乐下载` ·
`Windows 音乐下载软件` · `免安装音乐下载器` · `Tauri 桌面应用` · `Python 音乐下载脚本`

## 免责声明

本项目仅供个人学习与研究使用。使用者应尊重音乐版权，下载内容请勿用于商业用途或传播，并建议及时删除测试内容、支持正版。因使用本项目产生的任何法律问题由使用者自行承担。
