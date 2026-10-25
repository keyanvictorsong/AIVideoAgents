# 文案与话题 Agent

从本地视频的字幕、画面或你提供的内容描述，准备抖音和小红书文案。源码包含视频证据提取、事实校验、候选文案导出、参考资料导入与可选在线采集；本模块不发布作品。

这是通用分享版。请使用自己的素材，按当次需求指定主题、语气、长度和角度。包内测试只用虚构示例，不附带原使用者的文风、历史文案、采集结果、账户或登录会话。

## 环境

Python 3.11+，视频分析需要 FFmpeg/ffprobe。macOS OCR 需要 Swift 编译器和系统 Vision；其他系统可自行安装带 `chi_sim` 与 `eng` 语言数据的 Tesseract。核心 Python 模块只用标准库。可选网页采集需要 Playwright 及浏览器，按分享包根目录的安装说明安装。

使用分享包根目录虚拟环境中的 `dyxhs`；也可先在本目录运行 `python -m pip install -e .`。以下命令均使用安装后的入口：

```sh
dyxhs doctor
dyxhs run '/你的素材目录/视频.mp4' --output '/你的工作目录/02 TitleTagoutput' --skip-collect
```

`--skip-collect` 只跳过在线参考采集。视频仍会在本机提取证据。没有字幕或希望跳过画面 OCR 时，可传入自己核对的描述：

```sh
dyxhs run '/你的素材目录/视频.mp4' --skip-ocr \
  --content '逐步演示桌面整理，展示整理前后的区别。' \
  --output '/你的工作目录/02 TitleTagoutput' --skip-collect
```

## 不配置模型 API 的用法

将素材路径交给能读取本地文件、运行命令的 AI 编程助手，并让它按本目录 `AGENTS.md` 执行。首次 `run` 会生成 `generation_request.md`、字幕和证据。助手据此制作 `reviewed_facts.json` 与 `reviewed_copy.json`，然后复跑：

```sh
dyxhs run '/你的素材目录/视频.mp4' \
  --output '/你的工作目录/02 TitleTagoutput' \
  --reviewed-facts '/你的工作目录/02 TitleTagoutput/reviewed_facts.json' \
  --reviewed-copy '/你的工作目录/02 TitleTagoutput/reviewed_copy.json' \
  --skip-collect
```

事实文件必须绑定当前视频及证据的 SHA-256，并引用实际证据 ID。程序不会凭文件名直接写文案。未配置模型且未提供审核文件时，它会停在待助手整理状态。

想让程序调用你自己的模型，可把 `config.env.example` 复制成本目录 `.env`，自行填写兼容 chat-completions 的模型地址、名称及自己的密钥。配置后运行 `run` 可能向该地址发送字幕与抽帧；只在你选择的服务上使用。留空时不会启用模型。分享版没有密钥或默认服务账户。

## 参考采集与导入

根据本次素材给出查询词；网页登录由收件人在新建的专用会话里自行完成。平台页面变化、登录或验证码可能导致采集不足。未取得真实证据时，标签只标记为相关候选。

```sh
dyxhs collect --query '本次内容相关检索词' --output '/你的工作目录/参考'
dyxhs login douyin
dyxhs login xiaohongshu
```

已有参考数据可通过 `--import-references '/你的参考.json'` 导入，也支持 CSV。JSON 可以是数组或 `{"samples": [...]}`；常用字段为 `platform`、`id`、`url`、`title`、`description`、`tags`、`published_at`、`collected_at`、`content_type`、`author_id`、`metrics`。缺失数据保持未知；历史数据保留实际采集时间。

在线采集会在本模块 `.local/profiles/` 生成你自己的会话，输出保存在你指定的目录。再次转发工具包前，请排除这些新生成文件。

## 输出与限制

主要结果是 `output.md`、`alternatives.md`、`output.json`、`generation_notes.md` 和 `douyin-copy.json`。最后一个文件可交给发布 Agent 的 `--copy-file`；它只交接文案和本期话题检索约束，不代表已选定标题或已发布。

无 API 的方式需要助手完成事实与文案审核。可选 ASR 函数需要收件人自己准备本地模型和运行环境，CLI 未默认开启 ASR。参考采集不保证达到目标数量；程序中的长度和数量是本地预算，发布前以实际页面和当次需求为准。

退出码 `0` 表示该命令达到要求，`2` 表示已有部分产物但仍待审核或参考不足，`1` 表示错误；结合 `run_status.json` 判断。

离线测试：

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m unittest discover -s tests -v
```
