# wxpush —— 把文本消息推送到微信 / QQ

一个可以直接跑的推送模块：配置好凭证，一行代码把消息发到你的微信（个人 / 群聊）或 QQ。

```
wechat_pusher/
├── send.py                 命令行入口（计划任务 / crontab 直接调它）
├── demo.py                 可直接运行的调用示例（含真实错误码体检）
├── config.yaml             当前生效的配置（已预置可用通道）
├── config.example.yaml     配置模板，7 个通道注释齐全
├── requirements.txt        requests + PyYAML（websocket-client 仅 tools/ 需要）
├── tools/
│   └── qq_fetch_openid.py  抓 QQ 机器人的 group_openid（走 QQ 通道必用，一次性）
└── wxpush/                 模块本体
    ├── __init__.py         对外接口
    ├── errors.py           异常体系（决定"该不该重试"）
    ├── retry.py            重试策略（指数退避 + 平台冷却时间）
    ├── text_utils.py       按字节分片 / 截断（不是按字符！）
    ├── token_store.py      access_token 缓存（不缓存必踩坑）
    ├── settings.py         配置加载 + 环境变量展开
    ├── logging_setup.py    日志
    ├── pusher.py           统一入口：分发模式、去重、结果汇总
    └── channels/           7 个通道
        ├── base.py         通道基类（重试/分片/限流都在这）
        ├── wecom_bot.py    企业微信群机器人
        ├── wecom_app.py    企业微信应用消息（可指定成员）
        ├── wechat_mp.py    微信公众号模板消息
        ├── pushplus.py     PushPlus（个人微信）
        ├── serverchan.py   Server 酱 Turbo（个人微信）
        ├── qq_bot.py       QQ 机器人（QQ 群 / 单聊，官方接口）
        └── console.py      控制台（调试用）
```

---

## 先讲清楚两件事：个人微信没有官方 API，QQ 有

"把消息发给某个微信号"这件事，微信官方**没有开放接口**。网上所有声称能直接给个人微信
发消息的方案，本质都落在下面五条路里。先看清楚每条路的代价，再决定配置哪一个：

| 方式 | 能发给谁 | 申请难度 | 要花钱吗 | 消息最终出现在哪 |
|---|---|---|---|---|
| **PushPlus** | 个人微信；群组（一对多） | 极低，扫码即用 | 免费额度够个人用 | 微信（借道它的公众号） |
| **Server 酱 Turbo** | 个人微信 | 极低，扫码即用 | 免费额度够个人用 | 微信（借道它的公众号） |
| **QQ 机器人** | QQ 群 / QQ 单聊 | 中，需注册 QQ 开放平台开发者、提审上线 | 不需要 | QQ |
| **企业微信群机器人** | 一个**群** | 极低，1 分钟 | 不需要 | 企业微信群；群里若有微信用户且是微信互通群，微信端也能看 |
| **企业微信自建应用** | **指定成员**（可逐个指定） | 中，需注册企业微信 + 建应用 | 不需要 | 企业微信 App；**成员关注「微信插件」后个人微信也能收到** |
| **微信公众号模板消息** | 指定 openid 用户 | 高 | 需认证服务号（约 300 元/年） | 微信服务号会话 |

**怎么选**（按大多数人的实际情况）：

- 只想**自己收到消息** → 用 `pushplus` 或 `serverchan`。扫码 30 秒搞定，消息真的进微信。
- 想推到 **QQ** → 用 `qq_bot`（官方接口）。但先读下面的 QQ 门槛，它不是"填个 key 就完事"。
- 想发给**一个群 / 几个人**（企业微信）→ 用 `wecom_bot`。
- 想**精确发给某个同事的企业微信**（按部门、按标签、带水印保密消息）→ 用 `wecom_app`。
- 已经有认证服务号，要给具体粉丝发 → 用 `wechat_mp`。

> **不做什么**：第三方"微信/QQ 机器人框架"（itchat、wechaty、ComWeChat、OneBot、
> NapCat、go-cqhttp、LLOneBot 等）走的是逆向客户端协议，违反平台用户协议、**有封号风险**，
> 本模块不实现、也不建议。上面六条路里，`qq_bot` 是 QQ 侧唯一的官方接口。

### QQ 机器人（`qq_bot`）的三条门槛，先读完再动手

它是官方接口、能主动推送，但和"扫码就能用"完全不是一个量级：

1. **要注册开发者并提审上线。** 正式环境下发**主动消息**（就是定时推送这种）要求机器人
   已上线；未上线会返回 `{"code":40034102,"message":"主动消息失败, 无权限"}`。
   开发期可以先把 `sandbox: true` 走沙箱环境（沙箱不校验 IP 白名单、不要求已上线）。
2. **用户可以单方面关掉主动消息。** QQ 客户端里的「允许主动发送」开关一旦关闭，
   消息必然失败，而且**返回体里不会说明是这个原因** —— 这是最容易被误判成"程序坏了"的一种。
3. **`group_openid` 反查不到。** 它不是 QQ 号，只能由平台在事件里下发。
   也就是**机器人必须先在目标群里收到一条消息**，你才能拿到它去发推送。
   仓库里的 `tools/qq_fetch_openid.py` 就是专门干这一步的。

如果你的目标只是"在我手机上收到消息"，说句实话：**PushPlus 更快也更省事**，
消息就是进你的微信。选 QQ 通常是因为你本来就在 QQ 群里协作。

> 注意用词：`wecom_app` 里的 `touser` 填的是**企业微信成员账号（UserID）**，不是微信号。
> 成员在企业微信里绑定了微信、并关注了「微信插件」之后，消息才会推到他的个人微信。
> 这套模块已经把配置项和报错信息都按这个现实写好了。

---

## 快速开始（3 步）

### 1. 装依赖

```bash
cd E:/workbuddytemp/股票/wechat_pusher
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt      # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # macOS / Linux
```

### 2. 配置一个通道

复制配置模板，然后**至少打开一个通道**：

```bash
copy config.example.yaml config.yaml
```

> 本仓库已附一份 `config.yaml`：**`pushplus` 通道已设为 `enabled: true`**，只需补上环境变量里的
> token 就能直接发；`qq_bot` 已备好但默认关闭，走完 QQ 的注册流程再打开即可（文件里写了切换步骤）。
> 若想从零开始，就用上面的命令从 `config.example.yaml` 重新复制一份（模板里所有通道默认 `enabled: false`）。

**最快的路径（PushPlus，推个人微信，30 秒）：**

1. 到 https://www.pushplus.plus 微信扫码登录 → 复制 token
2. 设置环境变量（Windows，设完**必须重开终端**）：

```bat
setx PUSHPLUS_TOKEN "你的token"
```

3. `config.yaml` 里 `type: pushplus` 那段确认是 `enabled: true`（仓库里已如此），无需其他改动

**想推到 QQ（`qq_bot`，需要先注册开发者）：**

```bat
:: 1) 到 https://q.qq.com 注册开发者 → 创建机器人 → 拿 AppID / AppSecret
setx QQ_BOT_APPID  "102xxxxxx"
setx QQ_BOT_SECRET "你的AppSecret"

:: 2) 把机器人加进目标群，@ 它一次；然后抓 openid（它不是 QQ 号，反查不到）
python tools\qq_fetch_openid.py --sandbox --once

:: 3) 把抓到的 group_openid 设成环境变量
setx QQ_BOT_GROUP_OPENID "抓到的值"
```

然后把 `config.yaml` 里 `pushplus` 改为 `enabled: false`、`qq_bot` 改为 `enabled: true`。
开发期先在 `qq_bot` 段设 `sandbox: true` 跑通，再改 `false` 走正式环境（**正式环境发主动消息
要求机器人已上线**，否则报 `40034102`）。

**企业微信群机器人**（若你确实用企业微信）仍是 1 分钟的路：建群 → 右上角 `...` → 群机器人 →
添加 → 复制 Webhook，只取 `key=` 之后那一段：

```bat
setx WECOM_BOT_KEY "693a91f6-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

对应 `config.yaml` 里加上：

```yaml
  - type: wecom_bot
    enabled: true
    name: "我的企业微信群"
    key: "${WECOM_BOT_KEY}"     # 或 webhook: "${WECOM_BOT_WEBHOOK}" 填完整地址，二选一
    msg_type: "markdown"        # markdown(4096字节) | text(2048字节，只有 text 能 @成员)
```

### 3. 自检 → 发送

```bash
python send.py --check                                    # 只验证凭证，不发消息
python send.py -t "测试" --text "hello from wxpush"        # 真发一条
```

`--check` 会**真实调用**取 token 接口来验证凭证 —— 这样"corpid 抄错一位"这种问题
能在正式跑之前就暴露，而不是等半夜推送失败才发现。

---

## 代码调用

### 最简写法

```python
from wxpush import send_text

report = send_text(
    title="山西焦煤 000983 ｜ 收盘摘要",
    text="收盘 6.53 ▲+0.62%\n成交 49.40万手 / 3.22亿",
)
print(report.summary)
# -> （all）成功: 我的微信, 运维群
```

### 需要处理结果时

```python
from wxpush import Pusher

with Pusher.from_config_file("config.yaml") as p:
    report = p.send("标题", text="正文", markdown="**加粗正文**")

    if report.any_ok:
        print("至少一个通道成功:", report.summary)
    elif report.should_retry:
        # 网络抖动之类的临时问题 -> 值得把整轮任务重跑一次
        print("可重试:", report.summary)
    elif report.needs_human:
        # 凭证错了之类 -> 重跑一万次也一样，该喊人了
        print("需要人工介入:", report.summary)

    for r in report.results:
        print(r.channel, r.ok, r.detail, f"分{r.chunks}片", f"尝试{r.attempts}次")
```

### 只发给指定通道 / 用 failover 模式

```python
# 只发企业微信应用消息；失败自动切到 PushPlus（按配置顺序）
report = p.send("告警", "磁盘使用率 92%",
                only=["wecom_app", "pushplus"], mode="failover")
```

### 带去重的发送（防计划任务重跑造成重复推送）

```yaml
# config.yaml
dedup:
  enabled: true
  ttl_seconds: 1800
```

```python
p.send("收盘摘要", report_text, dedup_key="2026-09-25|market_close")
# 30 分钟内用同一个 key 再调一次 -> 直接返回，不重复推送
```

---

## 命令行用法

```bash
# 直接发
python send.py -t "标题" --text "正文"

# 发文件内容（适合发报告）
python send.py -t "山西焦煤 收盘摘要" --text-file report.txt

# 把别的程序的输出直接推过来
python other_task.py | python send.py -t "任务结果" --stdin

# Markdown 正文（支持 markdown 的通道优先用它）
python send.py -t "标题" --markdown-file report.md

# 控制与调试
python send.py --list                       # 列出所有通道类型与各自的字节上限
python send.py --check                      # 配置 + 凭证自检
python send.py --print-config               # 打印生效配置（凭证已打码）
python send.py --only wecom_app --mode failover -t "t" --text "c"
python send.py --dry-run -t "t" --text "c"  # 只走流程，不发请求
python send.py --json -t "t" --text "c"     # 输出 JSON，便于被别的程序消费
```

**退出码**（计划任务里直接靠它判断成败）：

| 码 | 含义 |
|---|---|
| `0` | 至少一个通道成功（`require_all` 模式下为全部成功） |
| `1` | 全部通道失败 |
| `2` | 配置错误 / 用法错误 |

---

## 重试与异常处理

这是这套代码里最值得看的部分。核心设计：**把"重试有没有意义"编码进异常类型**，
而不是让调用方去猜各家平台的错误码。

| 异常 | 含义 | 处理方式 | 典型错误码 |
|---|---|---|---|
| `PermanentError` | 凭证错 / 参数错 / 无权限 | **立即放弃**，告警找人 | wecom `93000` `40013` `60011`；pushplus `903`；sct `40001`；qq `10004` `100016` `40034102` |
| `TokenExpiredError` | access_token 失效 | 作废缓存 → 重取 token → 重试 | wecom `40014` `42001`；mp `40001`；qq `11244` |
| `RateLimitError` | 触发限流 | 按平台给的冷却时间等待后重试 | `45009`；qq `100001` `22009` |
| `RetryableError` | 网络抖动 / 服务端 5xx / 未知 | 指数退避重试 | — |
| 其他所有异常 | requests 的各种网络异常等 | 一律按可重试处理 | — |

最后一行很关键：判断可重试用的是 `is_retryable()` 而不是
`getattr(exc, "retryable", False)` —— 后者会把 `requests.ConnectionError` 误判成
"不可重试"，于是一次网络抖动就让整条推送彻底失败。

**再强调一次各平台的判定方式不同，这是实测结论：**

| 平台 | 业务失败时的 HTTP 状态码 | 真正的判据 |
|---|---|---|
| 企业微信（全部接口） | **200** | body 的 `errcode` |
| Server 酱 | 400 | body 的 `code` |
| PushPlus | 200 | body 的 `code` |
| QQ 取 token | **200** | body 的 `code`（如 `10004`） |
| QQ 发消息 | 401 / 200 | body 的 `code`（如 `11244`）；注意 `code` 与 `err_code` 是**两个不同的值** |
| QQ（appId 畸形） | 502，**返回 HTML 不是 JSON** | 只能看状态码 |

所以判断成败**一律解析响应体**；只有 QQ 的鉴权失败和网关 5xx 是状态码本身有意义的例外。

### 退避策略

```
第 1 次失败后等 1 秒 → 2 秒 → 4 秒（base=1，指数增长，上限 cap=30）
平台明确给出冷却时间时优先照做：说等 60 秒就等 60 秒（上限 retry_after_cap=120）
每次等待都叠加 ±35% 随机抖动，避免多个通道在同一秒集中重试、再次被限流
```

为什么 `retry_after_cap` 要和 `cap` 分开：企业微信限流后要求等 60 秒，如果被
`cap=30` 截断成 30 秒，30 秒后重试**必然再被拒**，白白消耗一次重试次数。

### 实测验证过的错误码

`python demo.py --real` 会用**伪造凭证**真实调用各平台接口，验证错误分类是否与平台
实际返回一致（不会发出任何消息）。当前实测结果：

```
  通道           异常归类               错误码       可重试
  ---------------------------------------------------------
  wecom_bot    PermanentError     93000     否
  wecom_app    PermanentError     40013     否
  pushplus     PermanentError     903       否
  serverchan   PermanentError     40001     否
```

> 一个必须知道的细节：**企业微信在凭证错误、webhook 无效、token 失效时全部返回
> HTTP 200**，错误信息在 body 的 `errcode` 里。而 Server 酱反过来 —— 失败时返回
> HTTP 400，但 body 是结构化 JSON。所以判断成败**只能解析响应体，不能看 HTTP 状态码**。

---

## 三个容易踩的坑（都已处理）

### 1. 长度限制是**字节**不是字符

企业微信 text 类型上限 2048 **字节**，一个汉字占 3 字节，所以约 682 个汉字就到顶了。

```
一条 800 个汉字的正文：
  len(text)      =    800 字符   -> 和 2048 比，看起来没问题
  utf8_len(text) =   2400 字节   -> 实际已超限 17%
```

用 `len()` 判断会漏判、导致整条消息被拒。本模块用 `split_by_bytes()` 按字节切片，
优先在换行处断开，并保证不切断多字节字符。超过上限自动分片续发，调用方不用管。

### 2. access_token 必须缓存，而且要留安全余量

- 有效期 7200 秒，**每次调用接口都会把有效期重置为 7200 秒**（active 计时）
- **同一个应用重复 gettoken 会让上一个失效** —— 如果你在别处（另一个脚本、n8n）
  也取过这个应用的 token，你的会被挤掉，表现为周期性 `40014` 半夜突然收不到消息

所以本模块缓存 token 到磁盘（进程重启不用重取），提前 300 秒刷新，并在收到
`40014/42001` 时主动作废缓存、重试时重新获取。

### 3. 本机代理可能只对某些域名不通

实测环境里出现过：`HTTP_PROXY` 配着，代理连不上某域名（报 `ProxyError`），
但直连完全正常。`network.auto_disable_proxy: true`（默认开）会在检测到
`ProxyError` 时自动切直连重试一次。

---

## 通道配置速查

### `wecom_bot` —— 企业微信群机器人

```yaml
- type: wecom_bot
  enabled: true
  name: "运维群"
  webhook: "${WECOM_WEBHOOK}"      # 也可以只填 key: "..."
  msg_type: "markdown"             # text | markdown
  mentioned_list: ["@all"]         # @成员，仅 text 类型生效
```

限制：text 2048 字节 / markdown 4096 字节；**markdown 不支持 @**；每分钟最多 20 条。

### `wecom_app` —— 企业微信应用消息（可指定成员）★

```yaml
- type: wecom_app
  enabled: true
  name: "发给我自己"
  corpid: "${WECOM_CORPID}"
  corpsecret: "${WECOM_SECRET}"
  agentid: "${WECOM_AGENTID}"
  touser: "ZhangSan"               # 成员账号，多个用 | 分隔；@all 表示全部
  # toparty: "1|2"                 # 按部门；totag: "1" 按标签
  msg_type: "text"
  safe: 0                          # 1=保密消息（加水印、不可转发）
```

申请步骤：

1. 企业微信后台 → 我的企业 → 企业信息 → 复制**企业 ID** → `WECOM_CORPID`
2. 应用管理 → 自建 → 创建应用 → 复制 **Secret** → `WECOM_SECRET`，记下 **AgentId**
3. 该应用 → **可见范围**里把目标成员加进去（漏了报 `60011`）
4. 应用 → **企业可信IP** 加上服务器出口 IP（漏了可能报 `60020`）
5. **让对方（或你自己）关注「微信插件」**，否则消息只到企业微信 App，个人微信收不到：
   企业微信 App → 我 → 设置 → 新消息通知 → 微信插件 → 扫码关注

### `pushplus` / `serverchan`

```yaml
- type: pushplus
  enabled: true
  token: "${PUSHPLUS_TOKEN}"
  template: "markdown"
  # topic: "群组编码"       # 一对多：群组内所有人都会收到
  # to: "好友token"         # 一对一：发给指定好友

- type: serverchan
  enabled: true
  sendkey: "${SERVERCHAN_KEY}"
```

Server 酱单个 SendKey 有频率限制，本模块已内置 12 秒最小间隔自动排队。

### `qq_bot` —— QQ 群 / QQ 单聊（QQ 开放平台官方接口）

```yaml
- type: qq_bot
  enabled: true
  name: "我的QQ群"
  appid: "${QQ_BOT_APPID}"                  # 开放平台管理端
  clientsecret: "${QQ_BOT_SECRET}"
  group_openid: "${QQ_BOT_GROUP_OPENID}"    # 发到群（与 user_openid 二选一）
  # user_openid: "${QQ_BOT_USER_OPENID}"    # 发到单聊
  sandbox: false            # 开发期先设 true，走沙箱（不校验 IP 白名单、无需已上线）
  msg_type: "text"          # text(0) | markdown(2)
  # msg_id: "..."           # 被动回复用：5 分钟内有效，不占主动消息额度
```

**`group_openid` 从哪来：** 它不是 QQ 号，也**没有任何接口可以反查**，只能由平台在事件里下发。
也就是说机器人必须先在目标群里收到一条消息，你才能拿到它去发推送：

```bash
python tools\qq_fetch_openid.py --sandbox --once
# 然后在群里 @ 机器人 发一句话，终端会打印 group_openid
```

**这个通道最容易踩的三个坑：**

1. **`40034102 主动消息失败, 无权限`** —— 正式环境下机器人未上线，或用户关掉了
   「允许主动发送」。这两种原因**返回体是一样的**，只能靠"是否已经提审通过"来区分。
2. **`11244` 与 `40011027` 是两个不同的值** —— 同一个失败响应里 `code=11244`、
   `err_code=40011027`。文档没说明两者关系，本模块以 `code` 为主判据，报错里两个都带上。
3. **Sandbox 与正式环境的凭证/目标不通用** —— 沙箱只能操作沙箱群、沙箱单聊；
   拿沙箱的 group_openid 去正式环境发，会得到鉴权错误而不是"目标不存在"，比较难猜。
   本模块的 `check()` 会回显当前用的是哪个环境。

**成功响应的形态也和别家不同**：HTTP 200 且 body 里**根本没有 `code` 字段**（只返回 `id`/`timestamp`），
所以不能用 `code == 0` 判断成功 —— 写成 `if code != 0: raise` 会把所有成功都判成失败。

### `wechat_mp` —— 需要认证服务号

```yaml
- type: wechat_mp
  enabled: true
  appid: "${MP_APPID}"
  secret: "${MP_SECRET}"
  openid: "${MP_OPENID}"
  template_id: "${MP_TEMPLATE_ID}"
  url: ""                            # 点击消息的跳转链接
  field_limit: 200
  data_fields:                       # {title} {text} {date} {summary} 会被替换
    first: "{title}"
    keyword1: "{date}"
    remark: "{text}"
```

需要先把服务器出口 IP 加进公众号后台的 **IP 白名单**（漏了报 `40164`）。
个人订阅号没有模板消息接口权限；如果你只是"想收到消息"，用 pushplus 就够，不必折腾这个。

---

## 常见问题

**Q：`--check` 报 `暂缺必要配置项: token` / `缺少必要配置项: webhook`？**
对应的 `${...}` 环境变量没读到。报错里会**直接回显变量名和设置命令**，照抄即可。
最常见的两个原因：① `setx` 之后没**重开终端**（对已打开的终端不生效）；
② 变量名拼错。临时绕过可用 `set WECOM_BOT_KEY=xxx`（仅当前窗口有效）。
另外 `wecom_bot` 的 key 里若含 `?` `&` `#` 等字符，**用双引号包起来**再 setx。

**Q：`--check` 显示 `配置完整（未校验凭证有效性）`，是不是没验证？**
是。`wecom_bot` 用的群机器人 Webhook **没有独立的探活接口**，唯一的确认方式是真发一条
（`python send.py -t "连通性测试" --text "测试"`）。其余需要 access_token 的通道
（`wecom_app` / `wechat_mp`）会用 `verifying` 开关在自检时**真实去取一次 token**，
所以那两类的自检结果是有意义的。

**Q：企业微信报 `60011 调用方没有权限操作该成员`？**
成员不在应用的「可见范围」里。到应用设置里把 ta 加进去。

**Q：消息在微信里收不到，但企业微信 App 收到了？**
没关注「微信插件」。见上面 `wecom_app` 第 5 步。

**Q：`45009 接口调用超过限制`？**
企业微信群机器人限 20 条/分钟。本模块已内置 3.2 秒最小间隔；如果你同时跑了很多标的
和时段，需要合并消息而不是扩线程。

**Q：`40014 invalid access_token` 反复出现？**
有别的程序在同时取同一个应用的 token，把你的挤掉了。排查同一应用的其他调用方；
或换一个独立的应用。

**Q：想去掉 docker / CI 环境里的颜色和进度噪音？**
`send.py --quiet --log-file logs/send.log`，输出只进文件。

---

## 接入已有的行情推送项目

如果你已经有一套生成报告的程序（例如同目录下的 `jiaomei_tracker`），不需要改它的取数
和报告逻辑，只要把"发送"这一步换成 wxpush 即可：

```python
# 方式 A：直接用 wxpush 的配置（推荐）
from wxpush import Pusher

class WxpushNotifier:
    """把 wxpush 包装成 jiaomei_tracker 期待的 Notifier 接口：
       返回 [{"channel":..., "ok":..., "detail":...}] 列表。"""

    def __init__(self, config_path="wechat_pusher/config.yaml"):
        self._pusher = Pusher.from_config_file(config_path)

    def send(self, title, text, markdown=None):
        report = self._pusher.send(title, text, markdown)
        return [r.to_dict() for r in report.results]

    def close(self):
        self._pusher.close()
```

然后把 jiaomei_tracker 里构造 `Notifier` 的那一行换成 `WxpushNotifier(...)` 即可，
其余代码（去重、失败提醒、`--status`）都不用动。

**如果只想多一条"发给我自己"的通道**，在 jiaomei_tracker 的 `config.yaml` 里加：

```yaml
  - type: wecom_app
    enabled: true
    name: "发给我自己"
    corpid: "${WECOM_CORPID}"
    corpsecret: "${WECOM_SECRET}"
    agentid: "${WECOM_AGENTID}"
    touser: "ZhangSan"
    msg_type: "text"
```

但要注意：jiaomei_tracker 自带的 `notifier.py` **不区分可重试与不可重试错误**，
凭证错了也会重试满 3 次。wxpush 的 `errors.py` + `retry.py` 就是为这个问题写的 ——
两者的取舍是"改造成本"对"长期稳定性"，量力而行。

---

## 限制与风险

1. **第三方通道（PushPlus / Server 酱）依赖其服务可用性。** 它们是商业服务，
   有免费额度限制、有变更可能，消息要经过它们的服务器。重要通知建议同时配 2 个通道。
2. **企业微信有应用消息数量上限。** 报 `67203` 就是当日额度用完了。
3. **微信生态的接口随时可能调整。** 公众号模板消息这几年一直在收紧，
   请以官方文档为准，不要把关键业务绑死在单个通道上。
4. **合规。** 仅用于给本人或已获授权的成员推送，不得用于群发营销、不得骚扰他人。
   凭证请放环境变量，不要提交到代码仓库。
5. **`--dry-run` 不会发请求**，但 `--check` 会真实调用取 token 接口（这是它存在的意义）。
