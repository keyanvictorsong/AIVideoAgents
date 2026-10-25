---
name: talking-editor
description: Prepare and edit talking-head videos locally, with reviewed captions, optional titles and covers. Use for explicit TalkingEditor requests and established talking-head editing workflows. Sports footage uses the separate optional template; publishing uses the platform agents.
---

# TalkingEditor

引擎位于本技能目录相对路径 `../../00Code`；符号链接安装后先定位实际目录。安装及参数见 [引擎说明](../../00Code/README.md)。

## 输入与选择

使用收件人自己的视频和独立输出目录。按当前任务确定语言、剪辑强度、字幕和封面样式；没有个人历史默认项。已有明确标题直接采用；需要由用户挑选时展示候选，已授权自动选择时可根据内容完成选择。封面比例和风格由当前用户选择。

不能凭文件名编造内容。原片保持不变。只有真实可用的转写和源文件证据才可用于标题与文案。视频与封面有标题时保持一致。

## 执行

1. 在 `00Code` 运行 `./run_video.sh "/path/to/input.mov" --output "/path/to/result"`。读取 `content.txt`、`transcript.json`、`report.json`；检查语言和明显识别错误。只完成分析时标为 prepared。
2. 需要平台文案时，读取相邻 `02DYXHSTitleTag/AGENTS.md` 并按其入口运行，使用本条已审核字幕作为输入。标题和关键词根据实际内容及当前用户授权选择。
3. 运行 `./run_video.sh "/path/to/input.mov" --output "/path/to/result" --title "选定标题"`，需要时重复 `--keyword`。修改已完成作品时才用 `--overwrite`。复用同一素材的转写缓存。
4. 封面是可选的交付组成。需要时查看 `cover-reference.jpg`，模糊或闭眼可通过 `--cover-time` 换帧；按用户的排版、颜色及比例要求制作封面。当前助手有图片工具时按工具技能操作；否则接受用户自制图片。不声称终端脚本会自动画图，也不擅自接入付费 API。
5. 验收字幕、视频、音画剪点及封面。单图使用 `--finish --cover "/path/to/cover.png" --cover-title "选定标题"`；单图 PNG 必须与视频同比例。双图接口同时传 `--cover-vertical`（1080×1440）和 `--cover-horizontal`（1440×1080），可为 PNG/JPEG。始终传相同的输入和 `--output`。底层 `python -m talking_editor` 可用于只需视频/字幕的任务。

封面文字和人物一致性需要目视检查；引擎的验收不等于 OCR 或身份验证。只有视频及约定的交付项确实完成后报告完成。读取实际 `removed_seconds`，若为 0 就如实说明保留原时长。

## 交接

提供实际输出路径与需要的成片、字幕、封面。文案和发布可由其他 Agent 接手，使用收件人自己的账户和环境。此技能只处理本地文件，不自动发布。
