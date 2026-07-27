# TG 自动签到 (tg-checkin)

基于 [Telethon](https://github.com/LonamiWebs/Telethon) 的 Telegram 机器人自动签到工具。
以你自己的账号身份登录，模拟向各机器人发送签到命令（支持自动点击按钮），配合
GitHub Actions 即可实现每天全自动签到，无需服务器。

## 功能特性

- **扫码登录** — 用已登录的 Telegram App 扫二维码完成授权，无需短信验证码（国内手机号友好）
- **手机号登录** — 验证码优先发到 Telegram App 内，非短信
- **自动点击按钮** — 签到后自动点击回复消息中的 inline 按钮
- **数学验证题自动解答** — 机器人弹出加减乘除算式时，自动计算并点击正确答案
- **图片验证码自动识别** — 机器人弹出图片选择题时，用 Gemini AI 识别图片并选择正确答案
- **代理支持** — SOCKS5 / SOCKS4 / HTTP 代理，适配各种网络环境
- **多机器人** — 一个配置文件管理所有签到机器人
- **定时自动化** — GitHub Actions 每天定时执行，零服务器成本
- **结果通知** — 签到结果可通过 Telegram 消息推送到收藏夹
- **日志记录** — 本地日志 + CI 日志上传，方便排查问题

## 目录

- [工作原理](#工作原理)
- [前置准备](#前置准备获取-api-id-和-api-hash)
- [快速开始（本地运行）](#快速开始本地运行)
- [配置 GitHub Actions 定时自动签到](#配置-github-actions-定时自动签到推荐)
- [配置字段说明](#配置字段说明)
- [环境变量一览](#环境变量一览)
- [代理配置](#代理配置可选)
- [常见问题](#常见问题)
- [项目结构](#项目结构)

## 工作原理

1. 通过 Telegram 官方 MTProto API 以**用户身份**登录（不是 Bot 身份）
2. 登录后生成一个 `SESSION_STRING`，等同于登录凭证，后续无需再输验证码
3. 脚本按配置依次向每个机器人发送签到命令，等待回复，可自动点击按钮
4. 部分机器人会弹出数学验证题或图片验证码，脚本可自动计算/识别并选择正确答案
5. 用 GitHub Actions 的定时任务每天自动触发运行

> 签到本质就是"帮你给机器人发条消息"，所以兼容所有机器人，不存在接口不兼容的问题。

## 前置准备：获取 API ID 和 API Hash

`API_ID` 和 `API_HASH` 是 Telegram 颁发给应用的身份证，让程序能以第三方应用身份调用 Telegram API。每个账号注册后获得独立的凭证。

1. 浏览器打开 https://my.telegram.org ，用你的 Telegram 账号登录
2. 点击 **API development tools**
3. 填写应用名称（随便填），创建后获得 **api_id** 和 **api_hash**
4. 把它们记下来，稍后填入 `.env`

> 这只是创建一个"API 应用"记录，不会改变任何账号设置，也不影响日常使用。
> 它们不是账号密码，单独泄露风险有限，但仍建议保密。

## 快速开始（本地运行）

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
# Linux / macOS
cp .env.example .env

# Windows
copy .env.example .env
```

编辑 `.env`，填入你的 `API_ID` 和 `API_HASH`。

> 如果使用手机号登录方式，还需填 `PHONE`（手机号，含国家代码，如 `+8613800138000`）。
> 使用扫码登录则不需要填 `PHONE`。

### 3. 首次登录，获取 SESSION_STRING

```bash
python login.py
```

脚本会让你选择登录方式：

#### 方式一：扫码登录（推荐）

> **国内手机号收不到短信验证码？用这个方式，完全不需要验证码。**

```bash
python login.py --qr
```

1. 运行后终端会显示一个二维码
2. 打开手机 Telegram App → **设置 → 设备 → 扫描二维码**
   - iOS / 安卓：Settings → Devices → Scan QR Code
3. 扫描终端上的二维码，在手机上确认登录
4. 登录成功后 `SESSION_STRING` 会**自动写入 .env**，无需手动复制

> 如果账号开启了两步验证，扫码确认后会提示输入云密码，输入即可。
>
> 前提：你的手机上已经能正常使用 Telegram（通过代理等）。扫码登录是通过已登录的
> App 授权新设备，不经过短信。
>
> 如果手机上也没有登录过 Telegram，可以先在手机端配代理注册登录（部分运营商
> 可能仍能收到 TG 的语音验证码），之后再扫码登录到脚本。

#### 方式二：手机号 + 验证码

```bash
python login.py --phone
```

1. 输入手机号
2. **验证码会优先发到你的 Telegram App 内**（如果手机上已有 TG 登录）
   - 在 App 内查看来自 Telegram 官方的验证码消息
   - 只有在没有任何设备在线时，才会通过短信发送
3. 输入验证码（如设置了两步验证还要输入密码）
4. 登录成功后 `SESSION_STRING` 会**自动写入 .env**，无需手动复制

### 4. 配置要签到的机器人

```bash
# Linux / macOS
cp config.example.yaml config.yaml

# Windows
copy config.example.yaml config.yaml
```

编辑 `config.yaml`，按格式添加你的机器人。示例：

```yaml
bots:
  - name: "某签到机器人"
    username: "xxx_checkin_bot"
    command: "/checkin"

  - name: "需要点按钮的机器人"
    username: "yyy_bot"
    command: "/sign"
    click_button: "签到"   # 机器人回复后，自动点击包含"签到"二字的按钮

  - name: "需要解数学题的机器人"
    username: "zzz_bot"
    command: "/start"
    click_button: "签到"   # 先点击签到按钮
    solve_math: true        # 机器人弹出算式题时自动计算并选择正确答案

  - name: "需要识别图片的机器人"
    username: "www_bot"
    command: "/start"
    click_button: "签到"   # 先点击签到按钮
    solve_image: true       # 机器人弹出图片选择题时用 AI 识别并选择正确答案
```

> **如何知道签到的命令和按钮文字？** 手动给机器人发一次签到命令，看它回复的内容，
> 把命令和需要点的按钮文字填进去就行。

### 5. 运行签到

```bash
python checkin.py
```

运行结束后会打印签到结果汇总，同时写入 `checkin.log`。

## 配置 GitHub Actions 定时自动签到（推荐）

把项目推送到 GitHub 仓库后，做以下配置：

### 1. 添加仓库 Secrets

进入仓库 **Settings → Secrets and variables → Actions → New repository secret**，
依次添加以下 Secret：

| Secret 名称 | 必填 | 说明 |
|---|---|---|
| `API_ID` | 是 | 你的 api_id |
| `API_HASH` | 是 | 你的 api_hash |
| `SESSION_STRING` | 是 | login.py 输出的字符串 |
| `NOTIFY_CHAT_ID` | 否 | 签到结果通知，填 `me`（收藏夹）或你的数字 user id |
| `NOTIFY_MAX_LENGTH` | 否 | 通知消息精简阈值，默认 `50`。超过此长度的回复只发送关键行 |
| `CONFIG_YAML` | 否 | config.yaml 的完整内容，不想公开机器人列表时用 |
| `PROXY_TYPE` | 否 | 代理类型：`socks5` / `socks4` / `http` |
| `PROXY_HOST` | 否 | 代理服务器地址 |
| `PROXY_PORT` | 否 | 代理服务器端口 |
| `PROXY_USERNAME` | 否 | 代理认证用户名 |
| `PROXY_PASSWORD` | 否 | 代理认证密码 |
| `GEMINI_API_KEY` | 否 | 图片验证码识别用（Gemini），从 [aistudio.google.com/apikey](https://aistudio.google.com/apikey) 免费获取 |
| `GEMINI_MODEL` | 否 | Gemini 模型名，默认 `gemini-2.0-flash` |
| `OPENAI_API_KEY` | 否 | 图片验证码识别用（OpenAI），从 [platform.openai.com/api-keys](https://platform.openai.com/api-keys) 获取 |
| `OPENAI_MODEL` | 否 | OpenAI 模型名，默认 `gpt-4o-mini` |
| `OPENAI_BASE_URL` | 否 | OpenAI API 地址，可填兼容 OpenAI 格式的第三方 |
| `EMAIL_ENABLED` | 否 | 邮件通知开关，设为 `true` 启用 |
| `SMTP_HOST` | 否 | SMTP 服务器地址，如 `smtp.gmail.com` |
| `SMTP_PORT` | 否 | SMTP 端口，默认 `465` |
| `SMTP_SSL` | 否 | 是否使用 SSL，默认 `true`；设为 `false` 时用 STARTTLS |
| `SMTP_USERNAME` | 否 | 发件邮箱账号 |
| `SMTP_PASSWORD` | 否 | 发件邮箱密码或应用专用密码 |
| `SMTP_FROM` | 否 | 发件人地址，默认同 `SMTP_USERNAME` |
| `SMTP_FROM_NAME` | 否 | 发件人显示名称 |
| `EMAIL_TO` | 否 | 收件人地址，多个用逗号分隔 |

> `NOTIFY_CHAT_ID` 填 `me` 表示发到你的"收藏夹"；填你的数字 user id 则发到与你的私聊。
> 获取 user id：向 `@userinfobot` 发消息。

> GitHub Actions 的服务器在海外，通常可以直连 Telegram，**一般不需要配置代理**。

### 2. 关于 config.yaml

两种方式任选其一：
- **直接提交 config.yaml 到仓库**（机器人用户名不算敏感信息，推荐这种，简单）
- 或者不提交，把内容放进 `CONFIG_YAML` Secret（已配置 workflow 自动读取）

### 3. 手动测试

进入仓库 **Actions** 页面 → 左侧选 **TG Checkin** → 右侧 **Run workflow** 手动触发一次，
确认能正常运行。签到日志可在运行记录的 Artifacts 中下载 `checkin-log` 查看。

### 4. 修改定时时间

默认每天北京时间 08:00 执行。如需修改，编辑
`.github/workflows/checkin.yml` 中的 `cron`：

```
北京时间 = UTC + 8
北京时间 08:00 → cron: "0 0 * * *"
北京时间 09:00 → cron: "0 1 * * *"
北京时间 00:05 → cron: "5 16 * * *"  （即 UTC 前一天 16:05）
```

> GitHub Actions 定时任务最多延迟十几分钟，属正常现象。
> 如需更精确定时，可在 cron 中加随机延迟（如 `0 0 * * *` 改为多条不同时间的 cron）。

## 配置字段说明

`config.yaml` 中每个机器人支持的字段：

| 字段 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `name` | 否 | username | 显示名称，仅用于日志 |
| `username` | 是 | - | 机器人用户名，不带 @ |
| `command` | 否 | `/checkin` | 签到命令 |
| `click_button` | 否 | - | 回复后自动点击的按钮文字（模糊匹配） |
| `solve_math` | 否 | `false` | 设为 `true` 时自动解答机器人弹出的数学验证题（+ - × ÷） |
| `solve_image` | 否 | `false` | 设为 `true` 时用 AI 自动识别机器人弹出的图片验证码 |
| `ai_provider` | 否 | 自动 | AI 提供商：`gemini`（默认）或 `openai`，不填则自动选择已配 Key 的 |
| `ai_model` | 否 | 见说明 | 指定模型名。Gemini 默认 `gemini-2.0-flash`，OpenAI 默认 `gpt-4o-mini` |
| `timeout` | 否 | 30 | 等待机器人回复的超时秒数 |
| `delay` | 否 | 2 | 发送命令前的延迟秒数（防限流） |
| `debug` | 否 | `false` | 设为 `true` 时输出机器人回复中的所有按钮文字 |
| `wait_final` | 否 | `false` | 设为 `true` 时在未收到引用回复的情况下继续等待任意第二条消息。通常不需要，脚本会自动检测引用签到命令的回复消息 |

## 环境变量一览

所有环境变量在 `.env`（本地）或 GitHub Secrets（CI）中配置：

| 变量 | 必填 | 说明 |
|---|---|---|
| `API_ID` | 是 | Telegram API ID，从 [my.telegram.org](https://my.telegram.org) 获取 |
| `API_HASH` | 是 | Telegram API Hash，从 [my.telegram.org](https://my.telegram.org) 获取 |
| `SESSION_STRING` | 是 | 登录凭证，运行 `login.py` 后获取 |
| `PHONE` | 否 | 手机号（含国家代码），仅手机号登录方式需要 |
| `NOTIFY_CHAT_ID` | 否 | 签到结果通知目标，填 `me` 或数字 user id |
| `NOTIFY_MAX_LENGTH` | 否 | 通知消息精简阈值（字符数），默认 `50`。超过此长度的机器人回复，邮件/Telegram 通知只发送开头关键行 |
| `CONFIG_FILE` | 否 | 配置文件路径，默认 `config.yaml` |
| `PROXY_TYPE` | 否 | 代理类型：`socks5` / `socks4` / `http`，默认 `socks5` |
| `PROXY_HOST` | 否 | 代理服务器地址，留空则不启用代理 |
| `PROXY_PORT` | 否 | 代理服务器端口 |
| `PROXY_USERNAME` | 否 | 代理认证用户名 |
| `PROXY_PASSWORD` | 否 | 代理认证密码 |
| `PROXY_RDNS` | 否 | 是否远程 DNS 解析，默认 `true` |
| `GEMINI_API_KEY` | 否 | Google Gemini API Key，图片验证码识别需要。从 [aistudio.google.com/apikey](https://aistudio.google.com/apikey) 免费获取 |
| `GEMINI_MODEL` | 否 | Gemini 模型名，默认 `gemini-2.0-flash`，也可用 `gemini-2.5-flash` 等 |
| `OPENAI_API_KEY` | 否 | OpenAI API Key，图片验证码识别的备选方案。从 [platform.openai.com/api-keys](https://platform.openai.com/api-keys) 获取 |
| `OPENAI_MODEL` | 否 | OpenAI 模型名，默认 `gpt-4o-mini`，也可用 `gpt-4o` 等 |
| `OPENAI_BASE_URL` | 否 | OpenAI API 地址，默认 `https://api.openai.com/v1`，可填兼容 OpenAI 格式的第三方 |
| `EMAIL_ENABLED` | 否 | 邮件通知开关，设为 `true` 启用 |
| `SMTP_HOST` | 否 | SMTP 服务器地址 |
| `SMTP_PORT` | 否 | SMTP 端口，默认 `465` |
| `SMTP_SSL` | 否 | 是否使用 SSL，默认 `true` |
| `SMTP_USERNAME` | 否 | 发件邮箱账号 |
| `SMTP_PASSWORD` | 否 | 发件邮箱密码或应用专用密码 |
| `SMTP_FROM` | 否 | 发件人地址，默认同 `SMTP_USERNAME` |
| `SMTP_FROM_NAME` | 否 | 发件人显示名称 |
| `EMAIL_TO` | 否 | 收件人地址，多个用逗号分隔 |

## 邮件通知（可选）

签到结束后可以把结果汇总发送到邮箱，适合不方便查看 Telegram 消息的场景。

### 配置方法

在 `.env` 中添加以下配置：

```env
EMAIL_ENABLED=true
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
SMTP_SSL=true
SMTP_USERNAME=your_email@gmail.com
SMTP_PASSWORD=your_app_password
EMAIL_TO=your_email@gmail.com
```

常见邮箱 SMTP 配置参考：

| 邮箱 | SMTP 地址 | 端口 | SSL | 密码说明 |
|---|---|---|---|---|
| Gmail | smtp.gmail.com | 465 | SSL | 需用[应用专用密码](https://myaccount.google.com/apppasswords)，不是登录密码 |
| QQ 邮箱 | smtp.qq.com | 465 | SSL | 需用授权码（设置 → 账户 → POP3/IMAP → 生成授权码） |
| 163 邮箱 | smtp.163.com | 465 | SSL | 需用授权码（设置 → POP3/IMAP → 开启 → 设置授权码） |
| Outlook | smtp-mail.outlook.com | 587 | STARTTLS | 需用应用专用密码 |

> **注意**：不要使用邮箱的登录密码，各大邮箱均要求使用「应用专用密码」或「授权码」。
> Gmail 需要先开启两步验证，然后生成应用专用密码；QQ/163 需先开启 IMAP 服务再生成授权码。

GitHub Actions 中则添加同名 Secret（`EMAIL_ENABLED`、`SMTP_HOST`、`SMTP_PORT` 等）即可。

## 代理配置（可选）

在国内或受限网络环境下，需要通过代理连接 Telegram。本项目支持 SOCKS5 / SOCKS4 / HTTP 代理。

### 本地运行

在 `.env` 中配置以下变量即可：

```env
PROXY_TYPE=socks5
PROXY_HOST=127.0.0.1
PROXY_PORT=7890
# PROXY_USERNAME=    # 可选，无认证则留空
# PROXY_PASSWORD=    # 可选
# PROXY_RDNS=true    # 远程 DNS 解析，默认 true
```

留空 `PROXY_HOST` 则不启用代理（直连），不影响原有行为。

常见代理软件默认端口：

| 软件 | 协议 | 地址 |
|---|---|---|
| Clash | socks5 | `127.0.0.1:7890` |
| V2Ray | socks5 | `127.0.0.1:1080` |
| V2Ray | http | `127.0.0.1:1087` |
| Shadowsocks | socks5 | `127.0.0.1:1080` |

> 注意：`login.py` 和 `checkin.py` 共享同一套代理配置，配置好后两个脚本都会走代理。

### GitHub Actions

GitHub Actions 的服务器在海外，通常可以直连 Telegram，**一般不需要配置代理**。

如果确实需要，在仓库 Secrets 中添加 `PROXY_TYPE`、`PROXY_HOST`、`PROXY_PORT` 等同名 Secret 即可，
workflow 已自动传递这些变量。

## 常见问题

### Q: 会被封号吗？

每天签到属于极低频操作，封号风险很小。建议：
- 不要把 `delay` 设得太小（保持 2 秒以上）
- 不要同时配置几十个机器人
- session string 是你的登录凭证，**切勿泄露或提交到公开仓库**

### Q: SESSION_STRING 会过期吗？

一般不会，除非你主动在 Telegram 设置里终止该会话。如果脚本报"无效或已过期"，
重新运行 `python login.py` 获取新的即可。

### Q: 登录时提示需要两步验证密码？

如果你给账号设置了两步验证密码，`login.py` 会要求输入，正常输入即可。

### Q: 国内手机号收不到验证码？

用**扫码登录**，完全不需要短信验证码：

```bash
python login.py --qr
```

终端会显示二维码，打开手机 Telegram → 设置 → 设备 → 扫描二维码，扫码确认即可。

> 前提：你的手机上已经能正常使用 Telegram（通过代理等）。扫码登录是通过已登录的
> App 授权新设备，不经过短信。
>
> 如果手机上也没有登录过 Telegram，可以先在手机端用代理注册登录（部分运营商
> 可能仍能收到 TG 的语音验证码），之后再扫码登录到脚本。

### Q: 某个机器人签到失败怎么办？

查看 `checkin.log` 中的错误信息。常见原因：
- 机器人用户名写错了
- 签到命令不对（手动确认一下）
- 触发限流（等待几小时后重试，或增大 `delay`）
- 按钮文字没匹配上（检查 `click_button` 是否和实际按钮文字一致）
- 数学验证题未识别（开启 `debug: true` 查看机器人回复原文，确认算式格式）
- 图片验证码未识别（确认 `GEMINI_API_KEY` 已配置，开启 `debug: true` 查看选项按钮）

### Q: 图片验证码识别失败怎么办？

1. 确认 `.env` 中已配置 `GEMINI_API_KEY`（从 https://aistudio.google.com/apikey 免费获取）
2. 确认 config.yaml 中对应机器人设置了 `solve_image: true`
3. 开启 `debug: true` 查看日志中的选项按钮文字和 AI 识别结果
4. 图片验证码只有 30 秒时间，正常情况下 AI 识别只需 3-5 秒，但如果网络不佳可能超时
5. Gemini API 免费额度为 15 次/分钟，每天签到一次绰绰有余

### Q: 本地运行时 Gemini API 连接失败？

在国内需要代理才能访问 Google API。本项目会自动复用 `PROXY_HOST`、`PROXY_PORT` 等代理配置
来连接 Gemini API，配置好 Telegram 代理后通常无需额外设置。

GitHub Actions 服务器在海外，不需要代理。

### Q: 想要签到结果通知怎么办？

两种方式：

**方式一：Telegram 消息通知**

在 `.env` 中设置 `NOTIFY_CHAT_ID=me`，签到结束后会把结果发到你的收藏夹。
GitHub Actions 中则配置同名 Secret。

**方式二：邮件通知**

在 `.env` 中配置 `EMAIL_ENABLED=true` 和 SMTP 相关变量（见上方「邮件通知」章节），
签到结束后会收到包含详细结果表格的 HTML 邮件。

两种方式可以同时启用，互不影响。

### Q: 连接超时 / 无法连接 Telegram？

很可能是网络问题，请配置代理。在 `.env` 中设置 `PROXY_HOST`、`PROXY_PORT` 等变量，
详见上方「代理配置」章节。日志中会打印当前使用的连接方式（直连或代理地址）。

### Q: GitHub Actions 运行失败？

1. 检查 Secrets 是否配置完整（`API_ID`、`API_HASH`、`SESSION_STRING` 必填）
2. 在 Actions 运行记录中下载 Artifacts 里的 `checkin-log` 查看详细日志
3. 如果是 `SESSION_STRING` 过期，重新本地运行 `login.py` 获取新的并更新 Secret
4. 如果是 `config.yaml` 找不到，确认已提交到仓库或配置了 `CONFIG_YAML` Secret

## 项目结构

```
tg-checkin/
├── checkin.py                    # 主签到脚本
├── login.py                      # 首次登录工具（扫码 / 手机号，生成 session string）
├── proxy.py                      # 代理配置模块
├── image_solver.py               # 图片验证码识别模块（Gemini Vision API）
├── email_notify.py               # 邮件通知模块（SMTP）
├── config.example.yaml           # 机器人配置模板
├── config.yaml                   # 实际配置（需自行创建，已 gitignore）
├── .env.example                  # 环境变量模板
├── .env                          # 实际环境变量（需自行创建，已 gitignore）
├── requirements.txt              # Python 依赖
├── .gitignore                    # Git 忽略规则（保护敏感文件）
└── .github/
    └── workflows/
        └── checkin.yml           # GitHub Actions 定时任务
```

## 免责声明

本工具仅供学习交流使用。使用本工具产生的任何后果（包括但不限于账号限制、封禁）
由使用者自行承担。请遵守 Telegram 的服务条款。
