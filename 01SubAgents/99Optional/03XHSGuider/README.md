# 可选：公开视频研究 Agent

此模块保留旧研究工具中可复用的采集、数据导入、配额筛选、逐条证据审核和离线 HTML 导出能力。它不属于默认剪辑／文案／发布流程，也不发布作品。已移除旧题材词、个人默认分类、历史选样和浏览器状态。

需要 Python 3.11+ 和分享包内的 `02DYXHSTitleTag`。核心 Python 代码使用标准库；在线采集另需 Playwright 和浏览器。安装主包后，在本目录可执行 `./guider`；安装本模块后也可使用 `dyxhs-guider`。

```sh
./guider doctor
./guider prepare --import-references '/你的真实参考.json' \
  --keyword '本批次关键词' --output '/你的工作目录/研究'
./guider render --output '/你的工作目录/研究' \
  --analyses '/你的工作目录/审核分析.json'
```

`prepare` 只处理本地导入，不联网。导入 JSON/CSV 的字段见主文案 Agent 的 README。首次会生成待助手填写的 `analysis_request.json`；助手应逐项关联原始字幕、画面或元数据证据，再用 `render` 合并审核结果。仅有元数据的条目保持初步分析，样本或内容证据不足时仍导出部分报告。

需要在线采集时，必须显式传入本次查询词。登录使用收件人自己的专用会话：

```sh
./guider login xiaohongshu
./guider run --query '本次参考检索词' --keyword '本次内容关键词' \
  --output '/你的工作目录/研究'
```

`--query` 控制检索，`--keyword` 控制导入/已采集内容的实际文本筛选。两者可重复。分享版没有默认查询词；未传 `--keyword` 则不限定题材。筛选默认合计 50 条、两平台配额各 25；时间、门槛、配额均是可调整的工具参数，不是平台标准。缺少某个平台的名额不会自动转给另一平台。

输出包括 `report.html`、`report_data.json`、`samples.json`、`selection.json`、`analysis_request.json`、`analysis_validation.json` 和采集状态。网页可以离线打开，外部作品链接需要网络。

本模块复用主文案 Agent 的采集 profile，因此同一 profile 不能被两个程序同时打开。`.local/`、登录会话和研究输出均由你首次使用后产生，后续分享前应排除。

macOS 另有可选 `scripts/chrome_reader.py`，只读取你明确选择的小红书研究标签中的公开字段，需要收件人自行开启 Chrome 的 Apple Events JavaScript 并授权系统自动化。它不自动接入主流程，运行 `python scripts/chrome_reader.py --help` 查看参数；不要将浏览器的个人会话作为工具包发给别人。

退出码 `0` 表示满足本命令要求，`2` 表示部分结果，`1` 表示错误。在线入口依赖平台页面，分享版只做离线验证，不保证实时可用。

离线测试（从本目录）：

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:../../02DYXHSTitleTag/src \
  python -m unittest discover -s tests -v
```
