# Video Archive Skill

让本地 AI 帮你分析视频、理解使用目的、推荐压缩参数、制作可对比的小样，在你确认后批量压缩并指导原片归档。

这是一个可安装的 Agent Skill，配套脚本负责重复执行和文件保护。没有图形界面，不需要模型 API Key；使用者通过自己的 AI 工具操作。视频留在本机或用户已经挂载的存储中。

## 使用流程

环境检查 → 缺失依赖安装指导 → 扫描分析 → 询问用途 → 推荐参数 → 少量小样 → 用户观看确认 → 批量压缩与校验 → 归档指导。

- 不预设所有人都应使用 CRF22。参数与实际素材、目标和设备相联系。
- 原片默认原位保留；用户选择后，才在成功验证后移动到归档子目录。没有删除原片功能。
- 单个 FFmpeg 顺序处理，支持安全暂停、后台运行和按文件续跑。
- 每批任务独立保存清单、计划、小样、用户确认与执行记录，便于另一 AI 接手。

## 安装到本地 AI

仓库：[MelonTse/video-archive-skill](https://github.com/MelonTse/video-archive-skill)。[下载 v0.1.0 安装包](https://github.com/MelonTse/video-archive-skill/releases/download/v0.1.0/video-archive-0.1.0.zip)，解压得到完整的 `video-archive` Skill 目录，再按下方方式安装。版本说明和已知限制见 [Releases](https://github.com/MelonTse/video-archive-skill/releases)。

需要源码、测试或参与维护时，可以克隆整个仓库：

```sh
git clone https://github.com/MelonTse/video-archive-skill.git
cd video-archive-skill
```

本仓库的唯一 Skill 位于 `.agents/skills/video-archive/`，包含全部运行资源。

**在 Codex 中使用整个项目：** 下载或克隆仓库，在这个仓库根目录打开本地任务。Codex 按当前官方说明发现 `.agents/skills` 中的项目技能；必要时重新打开任务，再显式使用 `$video-archive`。

**安装到自己的其他项目：** 把完整的 `video-archive` 目录放到那个项目的 `.agents/skills/`。已有同名 Skill 时先比较版本，避免直接覆盖。

**个人范围安装：** 想在不同项目使用时，将完整 Skill 目录安装到当前 Codex 官方文档列出的用户技能目录 `~/.agents/skills/`。不要同时在同一使用环境安装重复副本。旧版客户端路径可能不同，以其版本文档为准。

**其他本地 AI 工具：** 按该工具的 Agent Skills 安装方式使用同一目录；或明确让它读取 `SKILL.md` 并按流程操作。AI 必须具有本地文件访问、终端执行及源/目标目录权限。普通聊天窗口无法直接处理用户硬盘里的视频。未做其他客户端的安装和行为实测，不宣称通用兼容。

[Codex 官方技能说明](https://learn.chatgpt.com/docs/build-skills) · [Agent Skills 标准](https://agentskills.io/specification)

## 开始对话

> 使用 $video-archive 帮我处理指定目录的视频。先检查本地环境，缺什么就指导我安装。先分析素材，再问我的使用目标并推荐参数，做小样让我比较。等我确认后再批量处理，并指导我整理原片。

AI 会先获取实际路径，之后依据扫描结果询问用途、画质/体积/时间偏好和播放设备。不要把安装 Skill 当作用户已经批准编码或移动全部视频。

## 环境与首版范围

Python 3.9+、FFmpeg、ffprobe；所选 FFmpeg 构建需含 libx265 或 libx264，以及 `-fps_mode` 支持。运行依赖只有 Python 标准库和上述系统工具，doctor 不自动安装软件。

首版以 macOS 为验证目标，使用 POSIX 文件锁。Linux 尚未验证；原生 Windows 不支持此执行器。跨 AI 工具的工作流格式不等于跨操作系统兼容。

自动执行范围：BT.709 标签完整、单主视频流的 H.264/HEVC 4:2:0 视频；HEVC 保持原始 8/10-bit，H.264 兼容模式只接受 8-bit。保持分辨率、帧率、宽高比、方向和已知色彩信息，复制全部音轨。辅助数据、封面和章节不复制；有字幕的文件转人工复核。不能仅凭 BT.709 判定拍摄模式，已知或疑似 Log 先单独讨论。

HDR/HLG/PQ、不明色彩、不支持格式、严格目标体积、调色、缩放、音频重编码、硬件编码不在首版自动执行范围。先准确说明限制，再扩展经过验证的方案。

## 验证边界

自动检查覆盖 FFmpeg 返回结果、ffprobe 媒体结构/参数/时长/适用时的帧数，以及源文件 size/mtime；不等于内容哈希、全片解码或主观画质验收。播放体验由用户看小样确认。

压缩副本变小不等于磁盘释放空间：本项目保留原片，通常增加总占用。移动到归档子目录也不是备份。

## 开发与测试

```sh
uv venv --python python3 .venv
.venv/bin/python -B -m unittest discover -s tests -v
```

测试使用独立临时目录和少量合成视频，不读取个人素材；合成编码限制为一个线程。开发时的 Skill 元数据验证器需要 PyYAML，可用 `uv pip install --python .venv/bin/python 'PyYAML>=6,<7'` 安装到项目环境。最终验证结果见 `VALIDATION.md`。

用户任务目录、视频和日志不提交到仓库。源码与技能说明采用仓库 `LICENSE` 中的 MIT 许可；FFmpeg 等外部依赖单独安装，遵循其各自许可。
