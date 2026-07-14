# Sentinel-gh

每日运行在 GitHub Actions 上的个人 GitHub **Feed** 采集器（Collector，不是 Agent）。

契约形态对齐 [ElectQ/Soundwave](https://github.com/ElectQ/Soundwave)：`bundles/` 给下游（如 Megatron），`data/` 是内部归档。

- 采集**我关注的人（followees）**的公开活动（star / fork / created / release …）
- following 列表日快照 diff → **新 follow 了谁**
- 采集**我的首页 feed（received_events）**，聚合出**圈外热门**（`network_hot`）——关注圈**外溢**正在关注/参与的项目
- 与 **GitHub trending** 交叉标注
- 产出 **bundles**（下游契约）+ feed/pulse（内部/调试）

## 数据契约（下游取数 — 与 Soundwave 同模式）

| 文件 | 说明 |
| --- | --- |
| **`bundles/index.json`** | **入口 + 就绪标记**（`latest` / `days[]` / `sha256`） |
| **`bundles/YYYY-MM-DD.json`** | 该**北京日**发生的全部高信号 items |
| `data/feed/*` | 内部 feed 投影（调试/回放） |
| `data/pulse/*` | 聚合 digest |
| `data/events/*.jsonl` | 原始 followee events（含 Push；**非契约**） |
| `data/received/*.jsonl` | 原始首页 feed events（用于圈外热门聚合；**非契约**） |
| `data/follows/*.jsonl` | follow diff 归档（**非契约**） |

```
https://raw.githubusercontent.com/<owner>/Sentinel-gh/main/bundles/index.json
https://raw.githubusercontent.com/<owner>/Sentinel-gh/main/bundles/<date>.json
```

```bash
BASE=https://raw.githubusercontent.com/<owner>/Sentinel-gh/main/bundles
curl -s "$BASE/index.json" | jq '{latest, days: (.days|length)}'
curl -s "$BASE/$(curl -s "$BASE/index.json" | jq -r .latest).json" \
  | jq '.stats, (.items[0] | {id, who, who_url, action, target, target_url, text, at})'
```

去重键：`(source_id, id)` 或 `(source_id, external_id)`，`source_id = github_followee_feed`。

### Bundle 条目（`items[]`）— 分析用最小字段

每天读 **`items[]` 即可**，核心只有这些：

| 字段 | 含义 | 例 |
| --- | --- | --- |
| `id` | 稳定唯一 id | `event:11667242636` |
| `who` | 谁（你关注的人） | `safedv` |
| `who_url` | 其主页 | `https://github.com/safedv` |
| `action` | 动作 | `star` / `fork` / `follow` / `created` / `release` / `network_hot` |
| `target` | 目标名 | `0avx/0avx.github.io` 或用户 login |
| `target_url` | **仓库或主页链接（分析主入口）** | `https://github.com/0avx/0avx.github.io` |
| `text` | 一句话 | `safedv starred 0avx/0avx.github.io` |
| `at` | 事件时间 | `2026-07-12T06:57:07+00:00` |

**`network_hot`（圈外热门）是聚合 item,不是单个人的动作**:`who` 为空,`flags.is_aggregate=true`,细节在扩展字段 `network` 里(`outer_count` 圈外人数 / `outer_actors` / `by_kind` 参与类型分布 / `inner_count` 圈内是否也 star / `trending` / `languages`)。它来自你的首页 feed(`received_events`),表示「你关注的圈子**外溢**正在关注/参与的项目」。`follow` item 另带 `persona` 扩展字段。

兼容 Soundwave / Megatron 时另有别名：`external_id`=`id`，`author`=`who`，`url`=`target_url`（主链），`content`=`text`，`published_at`=`at`，以及 `links` / `refs` 等扩展字段。

默认 **不**把 `PushEvent` 放进 bundle（仍在 `data/events/`）。

### Pulse 核心板块

- `circle_hot` — 当日被 ≥2 个 followee star 的仓库（**圈内**共识）
- `circle_outer_hot` — 首页 feed 里被 ≥2 个**圈外**的人 star/fork/讨论的仓库（关注圈**外溢**热度；热度按 distinct actor 数计,含 star/fork/issue/PR/评论;标注圈内是否也有交叉 + trending）
- `trending_overlap` — followee star ∩ 全站 trending
- `stars` / `forks` / `follows` — 个体列表摘要
- `releases` / `new_repos` / `raw_counts`

## 隐私（P-C）

本仓库默认 **public**，并会提交：

- followee 的 star/fork 等公开活动
- **follow 边时间序列**（「你关注的人新 follow 了谁」）与 `state/following.json` 快照
- 你的**首页 feed 快照**（`data/received/`）与圈外热门聚合——均为可爬取的公开活动，但会暴露「你的 GitHub 首页看到什么」这一侧写。可用 `RECEIVED_ENABLED=0` 关闭。

这是可爬取的社交图信号。可用 `PUBLISH_FOLLOW_EDGES=0` 临时关闭 follow 边写入 feed/pulse 产品 JSON。

## 工作原理

### 调度

- `daily-pulse.yml` 每日 **UTC 21:00 = 北京时间 05:00** 运行（也可手动 `workflow_dispatch`）。
- GitHub 定时任务可能延迟数十分钟～数小时（与 Soundwave 同类）。
- 下游应用 **poll `bundles/index.json`**，不要死等时钟点。

#### Bundle 的分日口径

`bundles/D.json` = **事件 `created_at` 落在北京日 D** 的全部高信号 items —— 按事件**发生**时间分日，不是按采集批次。

因此：

- **幂等**：重跑、补采、回填都不会把事件挪到别的天；同一天重建多少次内容都一样。
- 05:00 的那次运行**同时收尾昨天、开启今天**（它覆盖昨天 05:00 ～ 今天 05:00）。所以 **`D` 在 `D+1` 凌晨跑完后才完整**；当天的 `latest` 是部分数据。下游若要「完整的一天」，取 `index.days[1]`（即 `latest` 的前一天）。
- 每次运行只重建被新事件**触及的那几天**（通常是昨天 + 今天），其余 bundle 不动。

### 采集

- followee 事件：`/users/{u}/events/public` **增量**（ETag + `last_event_at`），相对**上次成功运行**拉新事件。
  - 增量游标用 **`created_at` 时间戳，不能用 event id**：GitHub 的 event id 按类型分号段（Watch/Fork ~1.1e10，Push/Create ~1.4e10），新 star 的 id 会**小于**旧 push 的 id。曾按 `max(id)` 记水位线，导致游标被顶进 Push 号段后永久吞掉 star/fork。
- following diff：`/users/{u}/following` 日快照；**仅 full↔full 才 emit 边**。
- 首跑 following 只建 baseline，不刷假 follow。
- 首页 feed：`/users/{me}/received_events` **增量**（同样用 `created_at` 时间戳游标）。**⚠️ 该端点只保留最近 ~300 条事件**,活跃账号下一次日更覆盖的时间窗远小于 24h——圈外热门是**尽力而为**的信号,不是完整时间线。想覆盖更全需提高采集频率。
- trending 消费 [antonkomarev/github-trending-archive](https://github.com/antonkomarev/github-trending-archive)。
- 结束后 bot 提交 `bundles/` + `data/` + `state/`。

### 收集内容与排序

**进入 bundle 的高信号类型（默认）：**

| kind | 来源 | 说明 |
| --- | --- | --- |
| `star` | WatchEvent | 关注的人 star 了仓库 |
| `fork` | ForkEvent | fork 了仓库 |
| `follow` | following 日 diff | 新关注了谁（无精确到秒，`daily_window`） |
| `created` | CreateEvent(repository) | 新建仓库 |
| `release` | ReleaseEvent | 发版 |
| `network_hot` | received_events 聚合 | **圈外热门**:首页 feed 里被 ≥2 个圈外的人 star/fork/讨论的仓库(聚合 item,非单人动作) |

**不进 bundle、只进 raw：** `PushEvent`、普通 Issue/PR 评论等（噪声）。received_events 里 followee 自己的活动也不重复进 bundle（已由 followee 采集覆盖）。

**排序：** `at` / `published_at`（事件时间）**从新到旧**；同秒再按 kind 优先级（network_hot > release > created > star > fork > follow）。`network_hot` 优先级最高,作为当日聚合摘要排在最前。

### 能否覆盖「过去 24 小时」时间线？

| 能力 | 说明 |
| --- | --- |
| ✅ 日更增量 ≈ 上一跑～本跑 之间的新公开动态 | 每天 5 点跑一次时，窗口约 **24h**（漏跑则更长，上限见下） |
| ✅ 高信号时间线（star/fork/follow/…） | 对齐 Dashboard 里常见的 star/fork 类，不是 1:1 复刻首页 HTML |
| ⚠️ 每人 API 约保留 90 天 / 最多约 300 条公开事件 | 某人一天刷爆 300 条时，最旧的可能挤出窗口（极少见） |
| ⚠️ 私有仓库 / 非公开活动 | 抓不到 |
| ⚠️ follow 无事件时间戳 | 只能知道「相对昨天名单变了」，精度是日窗 |
| ⚠️ 首跑 / 新 follow 的人 | 事件只回溯约 24h；following 首日只 baseline |

结论：**在每日成功运行的前提下，可以稳定覆盖你关注的人过去约 24 小时的公开高信号动态**；不是实时流，也不是 100% 等同 GitHub 登录态首页 Feed。

## 配置

1. 生成 PAT（fine-grained 即可，需读 following / 公开用户信息）。
2. 仓库 Settings → Secrets → Actions 添加 `GH_PAT`。

### 可选环境变量

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `FEED_ENABLED` | `1` | 是否写 feed |
| `FEED_KINDS` | `star,fork,follow,release,created,network_hot` | Feed kind 白名单 |
| `RECEIVED_ENABLED` | `1` | 是否采集首页 feed（received_events）并算圈外热门 |
| `FOLLOWING_ENABLED` | `1` | 是否采集 following diff |
| `FOLLOWING_MAX_PAGES` | `10` | 每人 following 最多页数（×100） |
| `FOLLOWING_ONLY_ACTIVE` | `0` | 为 1 时只刷新当日有事件的 followee |
| `PUBLISH_FOLLOW_EDGES` | `1` | 是否把 follow 边写入 feed/pulse |
| `GH_USER` | — | 本地无 PAT 时用公开 following 列表冒烟 |

## 本地开发

```bash
uv sync
export https_proxy=http://127.0.0.1:7890 http_proxy=http://127.0.0.1:7890 all_proxy=socks5://127.0.0.1:7890  # 可选
GH_PAT=xxx uv run python -m sentinel.run            # 全链路
GH_PAT=xxx uv run python -m sentinel.collectors.followees
GH_PAT=xxx uv run python -m sentinel.collectors.following
# 仅用本地归档重建 feed（指定 UTC 日）
FEED_DATE=2026-07-11 uv run python -m sentinel.analyzers.feed
```

## 设计文档

见 [`docs/design-feed-radar.md`](docs/design-feed-radar.md)。

## Roadmap

- Star/fork 整理与 LLM 分析报告（独立 workflow 的非契约模块）
- 薄 MCP server（`sentinel-mcp`），供 Agent 宿主以 stdio 查询 feed/pulse
