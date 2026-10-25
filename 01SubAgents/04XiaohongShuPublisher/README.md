# 小红书发布准备 Agent（通用分享版）

接收本人的定稿视频、3:4 竖封面和标题正文，完成页面上传与封面校验后保留浏览器，由本人亲自点击最终发布。程序没有自动发布参数，也不生成文案或封面。

本包不包含原作者的登录状态、账号、个人设置、素材、截图、发布历史或内容喜好。首次运行由使用者登录自己的账号。

## 环境

推荐按分享包总目录 README 安装并激活统一 `.venv`。本模块要求 Python 3.11+、Pillow、Playwright 和本机 Google Chrome；依赖写在 `pyproject.toml`。也可通过 `python -m playwright install chromium` 安装 Chromium 后使用全局 `--channel chromium`。文件锁使用 `fcntl`，支持 macOS/Linux；Windows 需 WSL 或移植锁实现。

以下命令在本模块目录、已激活环境后执行；也可在总目录使用 `python tools/agent.py xhs ...` 转发同样的参数。macOS 的 `./xhs-publisher` 会优先寻找总目录统一环境。

## 首次配置和本人登录

先由 Agent 根据本人当前创作者后台页面适配 `selectors.example.json`，保存为自己的 `.local/selectors.json`。模板中的 `null` 不是可用选择器。封面控件的 `cover_trigger`、`cover_dialog`、`cover_input`、`cover_imported`、`cover_confirm` 和 `cover_ready` 六项均必需；预览应指向实际图片、画布或实际承载图片的 CSS 背景，不能指向占位卡。缺少六项配置时程序会在上传前停止。`publish_button` 仅作只读检查。

```sh
python -m xhs_publisher login
```

在专用窗口里由本人完成登录或验证。默认资料目录为本模块 `.local/browser-profile`，不会读取其他人的 Cookie 或日常 Chrome 资料。全局 `--profile '/本人专用资料目录'` 可指定本人已有专用目录。可在本机 `.local/settings.json` 设置 `browser_profile` 和 `creator_url`；只填写自己的值。大陆与海外创作者页面以官方对本人账号的实际跳转为准，程序支持两种 origin。不要把模板或某人的路由当作所有账号的通用配置。

程序在确认真实发布页后，可把登录恢复状态私密保存到该 profile 的 `.xhs-auth-session.json`（0600）；仅补缺失且未过期的状态，不延长有效期。分享包不带此文件，平台会话是否有效由实际页面决定。

## 准备本条任务并上传

替换所有示例路径与文案。`prepare` 只在本地验证、生成 manifest，不打开浏览器。

```sh
python -m xhs_publisher prepare '/你的任务/01/edited.mp4' \
  --cover '/你的任务/01/cover-vertical.jpg' \
  --title '本条已确认标题' --description '本条已确认正文' \
  --tags '相关话题一,相关话题二' --output '/你的任务/04'

python -m xhs_publisher --selectors .local/selectors.json \
  upload '/你的任务/04/manifest.json' --timeout 600
```

标题、正文和标签可直接取自 02 的小红书输出；本 CLI 没有自动读取 02 JSON 的参数，Agent 须读取后准确传入。标题、正文限长由本地校验和平台实际页面共同决定；不能默默改写已经确认的标题。封面只接受已有 3:4 图，视频和封面 SHA 用于防止任务期间换文件。

上传必须在本机交互式终端执行。出现 `READY_FOR_USER` 后保持终端和浏览器打开，由本人检查完整文案、封面和账号，亲自发布后再在终端按回车关闭。`awaiting_user_publish` 表示准备就绪，始终不等于平台已发布；手动提交后需只读核对作品管理，不重新上传来查状态。最终发布和任何平台验证都由使用者完成。

## 能力范围与重试

页面选择器需针对本人当前 UI 配置和维护，离线样例不能保证真实站点永远不变。封面校验比较文件 SHA、编辑和主页面预览及本地图片，容忍缩放与轻微压缩；实际文字与裁切仍需人工看一遍。正文中的 `#话题` 按文字填写；未确认平台生成实体时不声称已选择热门话题。

上传前创建 `manual-handoff.json`，并使用 profile 锁和视频 SHA ledger 阻止盲目重复上传。导航或登录在上传开始前失败可修正重试；上传或交接开始后必须先只读核对结果。原表单丢失后恢复需要绑定当前视频、本人明确未提交说明与只读核验记录，使用 jobs.authorize_repreparation 一次性授权，不删除标记冒充新任务。

## 离线验证与再次分享

```sh
PYTHONDONTWRITEBYTECODE=1 python tests/run_offline.py
```

此入口禁用网络且不启动浏览器，覆盖 CLI、素材校验、封面像素比较、去重和恢复授权。完整 `python -m unittest discover -s tests -v` 还包含会启动临时无头 Chrome 的浏览器/会话测试，平台 URL 全部被本地 fixture 截获；测试里的凭据为合成字符串，与真实账号无关。

使用后 `.local`、运行输出和截图会包含本人的身份或内容。再次分享只发送干净源码，不发送个人运行数据。
