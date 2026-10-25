# 抖音发布 Agent（通用分享版）

接收本人的成片、定稿双封面和本条文案，上传到本人登录的创作者页面。默认填写后等待人工检查；只有本次任务明确授权并显式使用 `--publish` 时才尝试提交。清单、文件校验、截图和重复提交保护均保留。

本包没有任何原作者账号、Cookie、浏览器资料、运行记录、素材或文案喜好。默认简介和话题为空，不自动选择任何合集；所有内容由使用者提供。

## 环境

推荐从分享包总目录安装统一 `.venv` 并激活。Python 3.11+；本模块使用 Pillow、NumPy、OpenCV 和 Playwright，依赖写在 `pyproject.toml`。抽帧需要系统 FFmpeg/ffprobe 和中文字体；直接复用双封面时不抽帧、不需要 FFmpeg。发布页面使用本机 Google Chrome；也可选择已通过 `python -m playwright install chromium` 安装的 Chromium（`--channel chromium` 放在子命令前）。当前文件锁使用 `fcntl`，支持 macOS/Linux；Windows 需 WSL 或移植锁实现。

以下命令在本模块目录、已激活统一环境后执行。也可在总目录使用 `python tools/agent.py douyin ...` 转发同样的参数。

```sh
python -m douyin_agent --help
python -m douyin_agent doctor
```

`doctor` 检查完整的抽帧和浏览器环境；即使只复用现有封面，它仍会报告缺少 FFmpeg。

## 首次配置和本人登录

首次使用需让 Agent 根据本人当前页面适配 `selectors.example.json`，保存为自己的 `.local/selectors.json`。模板中的 `null` 不是可用选择器。不要从别人电脑复制 `.local` 或浏览器目录。

必须配置 `cover_imported`（或分别配置 `cover_vertical_imported`、`cover_horizontal_imported`），以及 `cover_vertical_ready`、`cover_horizontal_ready`。这些定位应指向编辑器和主表单中的实际图片或画布；其余模板键可按当前页面补充。两步封面编辑器的 `cover_next` 定位“设置横封面”，`cover_confirm` 必须定位真正的保存完成按钮。缺少必要预览配置时，程序会在上传前停止。平台页面会变化，通用源码和离线样例不能代替本人的首次现场适配。

```sh
python -m douyin_agent login
```

仅在专用窗口中由本人登录或处理验证。登录状态以后保存在本模块 `.local/browser-profile`。`--profile` 可显式选择本人另一个专用目录，放在子命令前；不读取日常 Chrome 资料。

## 推荐：复用 01 的定稿双封面和 02 的文案

替换下面所有示例路径，在本机运行。竖图要求 3:4，横图要求 4:3；例如 1080×1440 和 1440×1080。程序复制原文件到本次输出目录并记录 SHA-256，不裁切、不叠字、不重新抽帧。

```sh
python -m douyin_agent prepare '/你的任务/01/edited.mp4' \
  --copy-file '/你的任务/02/douyin-copy.json' \
  --cover-vertical '/你的任务/01/cover-vertical.jpg' \
  --cover-horizontal '/你的任务/01/cover-horizontal.jpg' \
  --output '/你的任务/03'

python -m douyin_agent --selectors .local/selectors.json \
  upload '/你的任务/03/manifest.json'
```

两个封面参数必须成对提供；无效文件或比例不符会在创建任务前拒绝。`--copy-file` 读取 02 的标题、正文、已有话题和 `topic_selection`；显式 `--title`、`--description`、`--hashtags`、`--topic-query` 和 `--topic-count` 优先。原片 SHA 与剪辑成片 SHA 分开记录。`--collection '本人合集名称'` 可指定合集，必要时再加 `--require-collection`。

不使用 02 时也可通过 `--title`、`--description`、`--hashtags` 提供本条内容。不传封面参数时会抽帧生成双封面，默认 `--title-mode preserve` 保留画面、不叠新字；需要黑白标题时显式使用 `--title-mode add`，中文字体可用 `--font` 指定。`auto` 为 `preserve` 的兼容别名，不根据原作者颜色风格推断标题。无语音转录、动作语义理解或 OCR 保证，必须预览成品。

## 交接和提交

默认上传应在本机交互式终端运行。准备好后保持终端和浏览器打开，人工核对账号、完整文案、话题实体及两张实际封面；本人在网页完成操作后再在终端按回车关闭浏览器。不要用 `--no-review` 进行人工交接：它会直接关闭浏览器；非交互环境也不能保证页面留存。

本次明确授权自动提交后，可在 `upload ...` 或 `run ...` 后增加 `--publish`。按钮点击后如结果未知，保留 `publish-attempt.json` 和本地 ledger，先只读查看作品管理，不能重复执行提交。`filled` 只表示准备完成，人工点击后 CLI 也可能仍记为 `filled`；实际发布需核对平台记录。页面不明确、封面核验失败或必填项未通过时停止，不冒充发布成功。

抖音话题选择只使用平台实际可见候选及本条查询词；没有足够相关候选时可以少选。页面未显示统计时间或指标含义时，不声称近期热榜。封面文件/预览变更检查不代表文字语义或最终裁切必然正确。

## 离线验证

```sh
PYTHONDONTWRITEBYTECODE=1 python tests/run_offline.py
```

此入口禁用网络且不启动浏览器，覆盖文案交接、任务校验、去重、双封面复用/比例/成对参数，以及合成视频抽帧。02→03 交接测试需要同一分享包中的 02 模块。`python -m unittest discover -s tests -v` 还包含会启动临时无头 Chrome 的集成测试，所有页面请求均由本地 fixture 截获；它不登录或发布真实作品。

使用后 `.local` 会包含本人的登录状态；`runs`、自选任务目录和截图也会包含本人的内容。再次分享时只分享干净源码，不把这些运行数据一起发送。
