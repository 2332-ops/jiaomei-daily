# 焦煤日报（云端版）

山西焦煤（000983）每日行情日报，运行在 GitHub Actions 上，
家里电脑不用开机。三个时段（北京时间，周一至周五）：

- 09:35 开盘摘要（`market_open`）
- 10:00 上午十点摘要（`morning_ten`）
- 15:02 收盘定稿（`market_close`）

定时触发的实际执行时间可能有几分钟延迟（GitHub 排队），内容不受影响。
法定节假日由程序内部的交易日守卫（行情时间戳）自动跳过，无需维护日历。

## 结构

- `jiaomei_tracker/` — 取数、指标计算、报告生成、调度入口（`run.py`）
- `wechat_pusher/` — 推送模块（QQ 机器人等 7 通道），凭证走 `.env` / 环境变量
- `.github/workflows/daily.yml` — 云端调度

## 需要配置的 Secrets（仓库 Settings → Secrets and variables → Actions）

| Secret 名 | 内容 |
|---|---|
| `QQ_BOT_APPID` | QQ 开放平台 AppID |
| `QQ_BOT_SECRET` | QQ 开放平台 AppSecret |
| `QQ_BOT_USER_OPENID` | 接收人 openid（单聊） |

## 手动试跑

Actions 页 → 「焦煤日报」→ Run workflow → 选择时段。
交易日守卫会识别休市日并安静跳过（exit 0），QQ 机器人当前走沙箱环境
（`wechat_pusher/config.yaml` 中 `sandbox: true`），机器人提审上线后改 `false`
并按平台要求配置 IP 白名单（GitHub 托管 runner 的出口 IP 不固定，
上线前需要改用固定出口 IP 的运行方式，或保持沙箱）。

## 数据持久化

`jiaomei_tracker/data/tracker.db`（行情存档、资金流累积、发送去重日志）
通过 Actions 缓存跨运行保留；缓存 key 带 run_id，每次运行后保存新快照。
