# TalkingEditor

本地口播剪辑引擎，支持单条视频和文件夹批量处理。包含 Python API、命令行和供聊天助手使用的执行技能，不包含任何账户、令牌、模型缓存或个人素材。

## 能力

- 本地语音检测及词级转写；CPU 使用 faster-whisper，Apple Silicon 可选 MLX Whisper。
- 保守压缩长停顿，清理明确的水词；边界不可靠时保留音画。
- 导出字幕版和无字幕版 MP4、SRT、ASS、原始转写、剪辑计划及处理报告。
- 复用同一素材的转写缓存；处理显示旋转、可变帧率和 HDR 转 SDR。
- 可选顶部标题与关键词强调。字幕字体、字号、行数及标题由自己的任务配置决定。
- 可选运动字幕模块及模板：人工审核文字和位置，保留完整视频时间线与原音。

本包是 CLI 和处理库，没有交互时间线，也不依赖剪映或原作者账户。字幕转写、剪点自然度、封面文字仍需观看和校对。

## 安装

需要 Python 3.11–3.13，建议使用 3.12。FFmpeg 需要 `libx264`、`ass`、`zscale`、`tonemap`；使用 FFmpeg 7 或更新版本。安装工具和首次获取未缓存模型需要网络，之后模型缓存保存在自己的电脑中。

macOS 示例，在本目录中执行：

```sh
brew install python@3.12 ffmpeg-full
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
# Apple Silicon 可选加装 MLX 后端
.venv/bin/python -m pip install -e '.[mac,dev]'
```

Linux 在系统中安装上述 FFmpeg 能力、Python 和中文字体后：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Windows 可以使用 Python 和完整版 FFmpeg，环境中的 Python 位于 `.venv\\Scripts\\python.exe`。用 `python -m talking_editor` 运行底层引擎；`run_video.sh` 是 macOS/Linux 启动脚本。

程序会优先发现 Homebrew 的 `ffmpeg-full`。也可以通过 `TALKING_EDITOR_FFMPEG` 和 `TALKING_EDITOR_FFPROBE` 指定可执行文件。macOS 遇到特定 FreeType 加载问题时，会尝试本机兼容版本，只影响子进程；无法找到兼容库时会报错并停止。

不需要 API Key。首次 ASR 使用公共模型时可能下载模型文件，准备离线运行时可先下载并通过 `--model` 指定自己的模型目录。不附带模型文件。

如果已按分享包根目录的指南统一安装，直接从包根执行 `python3 tools/agent.py deliver "/path/to/input.mov" --output "/path/to/result"`，无需在子目录重复安装环境。`run_video.sh` 也会优先查找包根的 `.venv`，再查找本目录 `.venv`。

## 快速使用

以下 `/path/to/...` 均替换为自己的路径。含空格或中文的路径保留引号。

```sh
# 先分析转写、剪辑计划和字幕，不渲染视频
.venv/bin/python -m talking_editor "/path/to/input.mov" --output "/path/to/result" --analyze-only

# 根据实际内容设定标题并渲染；转写缓存会复用
.venv/bin/python -m talking_editor "/path/to/input.mov" --output "/path/to/result" --title "自己选择的标题" --overwrite

# 批量处理
.venv/bin/python -m talking_editor "/path/to/videos" --output "/path/to/results"

# 使用自己的配置和语言
.venv/bin/python -m talking_editor "/path/to/input.mov" --output "/path/to/result" --config config.example.json --language auto
```

未提供单文件 `--output` 时，默认写入原片旁的 `<文件名>_output`。文件夹处理必须指定输出目录。`--overwrite` 只用于明确要替换的派生输出；源文件本身有保护。需要重新识别时用 `--retranscribe --overwrite`。

`config.example.json` 提供技术参数示例。剪辑强度、是否去水词、字幕关键词、字体和标题都可以自行调整。默认中文可用 `--language auto` 改为语言检测。中文字体需自行安装：macOS 默认 Hiragino Sans GB，其他系统默认 Noto Sans CJK SC；示例不强制固定字体。默认字幕外观是可修改的技术预设。

## 聊天助手的标题及封面流程

```sh
# 准备转写、content.txt 和 cover-reference.jpg
./run_video.sh "/path/to/input.mov" --output "/path/to/result"

# 根据实际内容选择标题后渲染
./run_video.sh "/path/to/input.mov" --output "/path/to/result" --title "自己选择的标题" --keyword "重点词"

# 自己制作或用当前助手的图片工具生成封面，再验收
./run_video.sh "/path/to/input.mov" --output "/path/to/result" --finish --cover "/path/to/cover.png" --cover-title "自己选择的标题"
```

`--cover` 要求 PNG 与视频比例一致。需要常见的双封面交接时，用下列参数替换单图参数：

```sh
./run_video.sh "/path/to/input.mov" --output "/path/to/result" --finish \
  --cover-vertical "/path/to/1080x1440.png" \
  --cover-horizontal "/path/to/1440x1080.jpg" \
  --cover-title "自己选择的标题"
```

这两个尺寸是此双图验收接口的契约，可选择使用单图模式。封面可手工制作；脚本不会调用付费 API，也不会凭空生成标题或图片。`cover-prompt.txt` 是可修改的通用提示词，根据自己选定的风格调整。验收检查解码、尺寸、指纹及字幕时间，封面文字和人物一致性需人工检查。

此流程的 `delivery.json` 区分 `prepared`、`rendered`、`complete`。底层 CLI 可单独输出视频及字幕，不强制要求封面。平台文案可交接给相邻的 `02DYXHSTitleTag`；发布使用收件人自己的账号和对应发布 Agent。

## 输出与限制

常见输出：`edited.mp4`、`clean.mp4`、`captions.srt`、`captions.ass`、`captions.json`、`transcript.json`、`edit-plan.json`、`report.json`、`settings.json`。聊天交付流程另保存封面与 `delivery.json` 等记录。

输出会记录自己电脑的输入/输出路径、转写和原片指纹。以后再次转发代码时排除这些运行文件、自己的 `.venv`、模型缓存和素材。

ASR 可能误识别人名、术语或水词；程序不自动挑选重录句或修正口误。自然度依赖保守的时间边界与剪点密度规则，不能代替观看。HDR 使用静态色调映射，不应用 Dolby Vision 动态元数据；ASS 和视频保留样式，SRT 仅保留文字与时间。程序不会自动发布。

## 验证与复用

```sh
.venv/bin/python -m pytest -q
```

测试使用合成媒体与伪造识别后端，不下载模型，不操作发布账号。真实 ASR 和各接收设备上的完整环境仍需用自己的素材验证。

```python
from talking_editor import edit_video, edit_folder
edit_video("input.mov", "result", config={"language": "auto"})
edit_folder("videos", "results", config={"backend": "faster-whisper"})
```

运动字幕的独立用法见 [模板说明](templates/basketball/README.md)。
