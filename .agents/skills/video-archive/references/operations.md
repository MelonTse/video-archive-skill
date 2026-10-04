# 脚本操作与任务格式

所有命令由 AI 在用户授权的目录操作。先解析 Skill 的真实绝对路径；以下 `"<脚本>"`、`"<任务>"` 是说明中的占位符，执行时替换。参数使用独立命令参数并正确引用路径；不要将文件名或用户确认文本拼接成可执行 shell 代码。复杂回复可以用 Python `subprocess.run([...])` 参数数组传给脚本。

## 检查、初始化、扫描

```sh
python3 "<脚本>" doctor
python3 "<脚本>" init --job "<任务>" --source "/data/videos" --output "/data/compressed"
python3 "<脚本>" scan --job "<任务>"
```

任务目录必须新建，位于素材目录之外；输出可在源目录内的单独子目录，扫描会排除它。`init` 只创建任务目录与配置，不创建压缩输出目录。可在 scan 前编辑 `config.json` 的目录排除列表；默认排除 `.隐藏目录`、归档目录及常见历史压缩目录，不包含任何个人的年份或相机路径。

读取 `analysis.json` 看 Profile、容量和时长；读取 `manifest.json` 看逐文件属性、状态与原始 ffprobe。扫描报告错误不代表这些文件被成功纳入。缺失完整 BT.709 标签等项目会进入 REVIEW，不能强行选入。

## 记录目标、方案和代表样品

扫描后先询问使用者的目标，然后参考 [quality.md](quality.md)。从 `assets/plan.example.json` 创建任务内 `plan.json`，填入实际扫描得到的 Profile ID 和相对文件路径：

```json
{
  "goal": "家庭电视观看，优先减少副本体积，接受较慢编码",
  "rationale": "所选素材为 BT.709；用户确认播放设备支持 HEVC，并选择保留原片",
  "encoder": "libx265",
  "preset": "medium",
  "crfs": [18, 20, 22],
  "threads": 0,
  "profile_ids": ["实际扫描的Profile ID"],
  "samples": [
    {"relative_path": "trip/clip.MP4", "start_seconds": 15, "seconds": 30}
  ]
}
```

这里的目标和数值只是格式示例，不能当作用户回复。`threads: 0` 为编码器自动多线程；合成测试使用 1。一次任务一个编码器和 preset，可测试 1–4 个 CRF，通常 2–3 个；改变编码器/preset 需要新的样品。Profile 内相同参数不代表场景相同，可多选场景。每个所选 Profile 至少一个小样，单段最长 60 秒；短视频可使用完整时长。批量包含所选 Profile 中全部 ELIGIBLE 文件。

默认不因编码耗时长而终止 FFmpeg。用户确有需要时可在计划中加入 `timeout_seconds`，正整数表示单个批量视频的编码超时秒数，0 或省略表示不限制；超时会终止当前编码，下次需从头处理这个文件。正常暂停仍使用 pause，不通过超时实现。

在小样前确定 `config.json` 的 `archive_mode`（keep/move），记录用户选择；移动目标子目录由 `archive_folder` 指定。同一任务的源、输出、归档目录名、排除范围在扫描后不可变化；要改路径或范围另建任务。

## 测试、展示、确认

```sh
python3 "<脚本>" sample --job "<任务>"
python3 "<脚本>" plan --job "<任务>" --crf 22
```

`sample` 每次创建独立样品目录，更新 `samples.json` 和 `sample-report.md`，不删除旧样品。打开真实 reference 和候选供用户观看，不用编码成功代替画质判断。`plan` 只展示选定参数对应的输出映射，不进行编码或归档，可在用户确认前运行。

用户明确接受小样、选择 CRF 并同意所展示批量范围后：

```sh
python3 "<脚本>" approve --job "<任务>" --crf 22 --user-confirmation "用户本次真实确认的回复"
```

不要提前执行或自动填入示例确认。`approval.json` 保存实际回复和配置/清单/样品指纹，防止复用过期的确认。程序不能独立认证真人意图，AI 必须确保回复真实存在。同一条用户回复已覆盖选择与批量范围时无需再问。

## 后台执行、暂停和恢复

```sh
python3 "<脚本>" start --job "<任务>"
python3 "<脚本>" status --job "<任务>"
python3 "<脚本>" pause --job "<任务>"
python3 "<脚本>" resume --job "<任务>"
```

`start` 启动脱离当前终端会话的控制进程，不是系统服务；操作系统或 AI 沙箱仍可能终止子进程。PID 只表示启动请求，必须用 status、状态和日志确认执行；不承诺关机后继续。

安全暂停完成当前视频的编码、验证、归档及保存记录后退出控制器。状态 PAUSED 且 `controller_active=false` 才是暂停完成。`resume` 只清除暂停请求：控制器已经退出则再 start；若仍活动就让它继续，不启动第二个。电脑重启后先确认卷已挂载，再 resume/start。

`run --job ...` 为前台执行同一队列，供支持长期进程的工具使用。所有修改任务的操作共用文件锁；不能在运行中 scan/sample/approve 或改配置。不同任务不要指向同一批源文件和相同输出目录，当前锁只覆盖单一任务。

控制器已退出而状态仍为 RUNNING/INTERRUPTED 时，确认原因后可用原计划重新 run/start。已完成输出逐个复验，未完成的当前文件从头编码；原片归档有独立恢复记录。已有但无法与任务记录匹配的输出保留并报错，不覆盖。

参数或归档策略变化使样品指纹失效，需要重新测试确认；批次启动后不改变方案，另建任务。不要手工改确认指纹来绕过检查。

## 输出与范围

- `sample-report.md` / `samples.json`：候选的大小、耗时、结构核验和本地路径。
- `state.json` / `batch-plan.json`：续跑和文件映射。
- `summary.json` / `results.csv`：批次结果与归档异常。
- `commands.log` / `controller.log`：具体执行参数、进度与错误。

单文件失败不会删除原片；批量结束返回 COMPLETED_WITH_REVIEW 时明确列出需复核的项目。压缩副本只含主视频和全部音轨，不含辅助数据、封面或章节；含字幕文件整项转 REVIEW。输出 MP4，H.264 标记 avc1，HEVC 标记 hvc1。无法直接复制的音频导致失败，不自动重新编码。

默认仅结构和元数据校验，不提供内容哈希或全片解码检查。不同文件系统、长片、旋转/VFR 等情况应通过代表小样验证；失败时不要为了“通过”放宽校验。
