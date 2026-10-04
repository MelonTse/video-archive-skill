# 环境检查与缺失依赖

先识别操作系统、终端和已有工具。示例命令不是要求每个人重新安装。

1. 查找 Python：macOS/Linux 用 `command -v python3` 和 `python3 --version`。要求 3.9+。没有 Python 时不能调用 Python 检查脚本；先提供对应系统的安装指导。
2. 运行 `workflow.py doctor`：它只检查，不安装。输出具体路径、版本、编码器及功能支持。`ready_to_sample=true` 表示至少一个支持的编码器可用；实际小样还会检查计划选中的编码器。
3. FFmpeg 与 ffprobe 必须能在当前 AI 工具的 PATH 中找到。终端能找到而 AI 工具找不到时，检查实际执行环境；不要反复安装。
4. 编码期间所用磁盘必须挂载、可读写且有空间。网络盘断开要按任务状态恢复，不能换一个同名空目录当作原磁盘。

## 安装指导

- macOS 已有 Homebrew：缺 Python 时可用 `brew install python`；缺 FFmpeg/ffprobe 时可用 `brew install ffmpeg`。没有 Homebrew 时，提供 [Homebrew 官方安装说明](https://brew.sh/) 和 [Python macOS 安装包](https://www.python.org/downloads/macos/)，由用户选择；不要自动运行网上的一键 shell 安装脚本。
- Ubuntu/Debian 已确认包管理器可用时：`sudo apt update`，然后 `sudo apt install python3 ffmpeg`。管理员认证应交给用户；此发行版环境尚未完成项目实测。
- 原生 Windows：本版执行器使用 POSIX 文件锁，不支持直接执行批量任务。可指导准备 WSL/Linux 环境，但要明确仍需验证，不承诺与 macOS 行为相同。不要绕过环境检查。
- 自行获取 FFmpeg 时从 [FFmpeg 官方下载入口](https://ffmpeg.org/download.html) 开始；其构建是否含 libx264/libx265 要以 doctor 实测为准。硬件编码器不能直接替代这两者的 CRF 设置。

用户没有要求安装时，报告缺失项并给出具体方案；已经授权安装则直接在授权范围内完成，遵守本机权限要求。安装完成后复查，不擅自修改编码配置以绕过依赖问题。

运行 Skill 不需要 pip 包，不需要 API Key。仓库开发环境使用 `.venv`，`PyYAML` 仅用于开发时检查 Skill 元数据，不是用户压缩所需依赖。
