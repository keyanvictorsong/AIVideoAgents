# 视频剪辑与发布 Agent 分享包

把这个文件夹放到你自己的电脑，用熟悉的 AI 编程助手打开整个目录，让它先读 `AGENTS.md`。这里是可执行源码和工作流程；AI 助手、平台账号、API 服务和视频素材由你自己准备。

从私有 GitHub 仓库获取时，先由维护者为你的 GitHub 账号开通访问权限，再用仓库页面的 Code → Download ZIP 下载，或克隆仓库。克隆后可用 `git pull --ff-only` 获取更新；安装、登录和使用自己的素材只需在本机完成。详见 [GitHub 获取与更新](docs/GitHub使用与更新.md)。

## 包含什么

| Agent | 用途 | 目录 |
| --- | --- | --- |
| 剪辑字幕 | 口播转写、审核剪辑计划、剪辑、字幕、标题与封面；附可选运动字幕模块 | `01SubAgents/01TalkingEditor` |
| 标题文案 | 核对视频事实、采集参考、生成双平台标题/简介/标签与交接文件 | `01SubAgents/02DYXHSTitleTag` |
| 抖音发布 | 抽帧/复用封面、导入文案、上传填写、话题选择与防重复提交 | `01SubAgents/03DouyinPublisher` |
| 小红书发布 | 校验已有封面、上传视频、填写文案、交给本人最终发布 | `01SubAgents/04XiaohongShuPublisher` |
| 可选旧工具 | 参考内容采集与报告，独立使用 | `01SubAgents/99Optional/03XHSGuider` |

## 第一次使用

1. 使用 macOS/Linux，安装 Python 3.11–3.13、带字幕渲染能力的 FFmpeg（含 `ffprobe`），并准备可用的中文字体。Apple Silicon Mac 是原流程的主要环境；Linux 的浏览器窗口管理、字体及 OCR 可能需要调整。发布器使用 POSIX 文件锁，Windows 目前不能直接运行完整发布流程，剪辑/文案模块可按各自说明单独使用。
2. 在终端进入本文件夹，用受支持的 Python 执行 `python3.12 tools/install.py`；若 `python3` 已为 3.11–3.13，也可用它。安装会联网下载依赖及 Chromium，不会登录或上传。
3. 执行 `python3 tools/doctor.py` 查看本地依赖。首次语音识别可能需要下载模型；不包含发送者的模型缓存。
4. 使用自己在抖音和小红书的账号登录，见下方命令。登录状态只会在你本机新建的 `.local` 下保存。
5. 让助手阅读 [通用流程](docs/使用流程.md)，从你自己的一个视频开始。

```bash
python3 tools/agent.py edit --help
python3 tools/agent.py copy --help
python3 tools/agent.py douyin --channel chromium login
python3 tools/agent.py xhs --channel chromium login
```

`--channel chromium` 使用安装器下载的浏览器；如需本机 Chrome，可改为 `--channel chrome`。发布页面需要你自己的网络、登录及必要的验证。采集参考内容与创作者后台的登录可能各自独立。

标题文案支持“当前 AI 助手审核事实和文案”的方式，无需额外配置 API。可选模型接口只填写你自己的配置，见 02 目录的 `config.env.example`；本包未提供密钥或预设服务账户。

## 直接给 AI 助手的开场话

> 请先读这个项目的 AGENTS.md 和 docs/使用流程.md，检查环境，用我提供的视频完成剪辑、字幕、双平台标题文案和封面。我会另行告诉你视频类型、风格及目标平台。先给我看结果；我要求进入发布阶段后，再用我自己的账号上传填写。说明当前哪些步骤已经完成、哪些需要我操作。

## 使用边界

本包默认上传填好后交给本人检查并最终发布。抖音程序另有显式 `--publish` 能力，仅在本人已授权本次提交时使用；小红书程序保留本人最终点击。没有继承任何其他人的发布授权、日更任务或个人风格。

平台页面、登录验证和可见字数限制会变化，遇到不匹配时需调整选择器。原账号的本机页面配置未随包发送：首次使用需要按你当前页面完成封面入口、上传、确认和预览的选择器适配，尤其是小红书，见 03/04 的 README 与 `selectors.example.json`。这是源码工作流包，完整平台适配后才能实际使用上传流程。本次打包只做本地验证，没有登录平台或测试真实发布。ASR、标题事实、剪辑和封面应结合实际素材核对。

[隐私与再次分享](docs/隐私与再次分享.md) · [本地检查结果](docs/交付检查.md)
