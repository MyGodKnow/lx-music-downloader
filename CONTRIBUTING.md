# 贡献指南

感谢愿意帮忙！这个项目主要靠业余时间维护，任何形式的贡献都有帮助。

## 最有用的贡献

### 1. 反馈音源可用性

音源是社区维护的，经常失效。如果你发现：

- 某个音源在别的工具里能用、在这个项目里不能用
- 某个音源已经彻底失效（卡密过期、接口改了）
- 某个平台整体下载失败

请开 [Issue](https://github.com/MyGodKnow/lx-music-downloader/issues/new/choose)，
附上**音源名称**和**日志区的报错**。这类反馈最有价值——它直接决定下载成功率。

### 2. 报告 Bug

用 Bug 模板，把「复现步骤 + 日志 + 环境」写清楚。有日志基本都能定位。

### 3. 提功能建议

说清楚「你现在怎么绕过这个问题」，比单纯说「想要 XX 功能」有用得多。

### 4. 改代码

## 开发环境

```bash
# 图形界面
cd lx-tauri
npm install
npm run tauri dev

# 命令行引擎
cd lx-downloader
python lx_music_downloader.py --help
```

## 提交前自检

```bash
# 引擎单元测试
cd lx-downloader && python -m unittest discover -s tests

# Rust 测试 + lint
cd lx-tauri/src-tauri && cargo test && cargo clippy --all-targets -- -D warnings

# 前端类型检查
cd lx-tauri && npx tsc --noEmit
```

四项全绿再提 PR。

## 代码约定

- **注释写「为什么」，不写「是什么」**。踩过的坑、反直觉的地方必须写清原因，
  否则以后有人会「顺手优化」掉。
- 不要在同步的 Tauri 命令里做阻塞操作——Tauri 2 的同步命令跑在主线程，
  任何 `sleep`/网络/解压都会冻结窗口。用 `async` + `spawn_blocking`。
- 涉及用户文件的删除操作必须二次确认，且校验路径（`validate_source_name` / `safe_join`）。
- 提交信息用中文，说明「问题 → 原因 → 修复 → 验证」。

## 不要做的事

- 不要提交音源脚本（版权与合规风险，且本项目不捆绑音源）
- 不要提交 `dist/` `target/` `node_modules/` 等构建产物
- 不要在仓库里放大体积二进制文件（引擎走 `engine.zip` 单独构建）
- 不要为了「代码好看」重构已经踩过坑的逻辑，除非你确认那些坑不再存在

## 安全

发现安全问题（如音源脚本逃逸、路径穿越）请不要开公开 Issue，
直接发邮件到 114514woguizhidaoya@gmail.com。
