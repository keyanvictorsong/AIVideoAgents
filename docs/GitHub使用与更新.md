# GitHub 获取与更新

本项目作为独立的公开源码仓库维护。仓库只保存经过脱敏审核的通用源码、说明、空配置模板和合成测试；账号、登录会话、密钥、素材和个人偏好各自在自己的电脑保存。

## 朋友第一次获取

直接打开 [仓库页面](https://github.com/keyanvictorsong/AIVideoAgents) 或 [下载 ZIP](https://github.com/keyanvictorsong/AIVideoAgents/archive/refs/heads/main.zip)，无需登录或访问邀请。

有 Git 时，在自己的终端执行：

```sh
git clone https://github.com/keyanvictorsong/AIVideoAgents.git
cd AIVideoAgents
python3.12 tools/install.py
python3 tools/doctor.py
```

下载和克隆公开仓库无需 GitHub 身份验证；向仓库推送更改仍需写入权限，使用本人自己的 Git 客户端登录或系统凭据管理器，不把 token 写到仓库地址、源码或聊天中。没有 Git 时通过 ZIP 获取，再按 README 安装。

用 AI 编程助手打开整个 `AIVideoAgents` 文件夹，先读 `AGENTS.md`。把自己的视频交给助手，并说明视频类型、风格及目标平台。账户登录和封面页面选择器需要在本人电脑配置，安装完并不等于已经完成平台适配。

## 朋友获取后续更新

在克隆的项目目录执行：

```sh
git status --short
git pull --ff-only
```

如果新版修改了依赖，再运行 `python3.12 tools/install.py`。未跟踪且被忽略的本人 `.env`、`.local` 和任务素材默认不会纳入提交；忽略规则不保护已经被跟踪的文件，更新前应妥善备份自己的资料，且不要强制把这些路径加入仓库。个人风格可放在 `.local/preferences.md`，然后明确让助手使用，避免修改和提交共享 AGENTS。

如果改过已跟踪源码，先提交到自己的本地分支或备份修改，再更新。不要使用强制覆盖命令清除本地工作。使用 Download ZIP 的人应把新版解压到新目录，再按需要保留自己的配置和资料；不要把带账号资料的旧目录重新发给其他人。

## 维护者发布更新

日常原工程与这个分享仓库是两个独立目录，不会自动相互覆盖。更新时只把可复用能力整理到本仓库，重新排除原工程的个人设置与资料。也可以让助手执行：“把最新通用流程同步到分享仓库，脱敏检查后更新 GitHub。”

1. 修改和核对通用源码，运行与改动相关的离线验证。
2. 用 `git status --short` 和 `git diff` 审阅，只暂存明确审核的文件。新增模板也必须检查实际内容；`.gitignore` 不能证明文件中没有密钥。
3. 从暂存源码快照更新清单，再暂存清单并检查。命令见下方；检查工具不会自动把本机数据加入清单。

```sh
git add <逐个审核的源码或文档路径>
python3 tools/build_share.py --manifest-only
git add MANIFEST.json
python3 tools/check_share.py --repo
git diff --cached --stat
git commit -m "Update reusable video workflow"
git push origin main
```

`--repo` 检查 Git 索引里的实际内容，包括已暂存但工作树又被改回的文件；不会遍历或上传本人本机 `.git`、被忽略的配置及素材。清单应与本次暂存快照匹配。提交作者使用仓库专用通用身份，不继承原工程作者邮箱；GitHub 登录凭据始终由本机凭据管理器提供。

需要新的离线分享 ZIP 时，使用 `tools/build_share.py --help` 查看导出参数，导出位置放在仓库外。工具按审核后的文件名单导出，不能把整个用过的目录直接压缩。公开访问仅开放源码下载，不授予其他人仓库写入权限。
