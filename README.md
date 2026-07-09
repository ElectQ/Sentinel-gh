# Sentinel-gh

每日运行在 GitHub Actions 上的个人 GitHub 动态追踪器：采集**我关注的人（followees）的全部公开活动**，与 **GitHub trending** 做交叉标注，产出结构化的每日 pulse JSON，供下游系统拉取。

## 数据契约（下游取数）

| 文件 | 说明 |
| --- | --- |
| `data/pulse/latest.json` | 最新一天的 pulse（下游取数入口） |
| `data/pulse/YYYY-MM-DD.json` | 每日 pulse 存档 |
| `data/pulse/schema.json` | pulse 的 JSON Schema（契约，破坏性变更会递增 `schema_version`） |
| `data/events/YYYY-MM.jsonl` | 原始公开事件按月归档（append-only，裁剪过 payload；**非契约**，仅供后续分析） |

下游直接拉 raw 文件即可：

```
https://raw.githubusercontent.com/<owner>/Sentinel-gh/main/data/pulse/latest.json
```

pulse 核心板块：

- `circle_hot` — 当日被 ≥2 个 followee star 的仓库（圈子热点）
- `trending_overlap` — followee star 的仓库 ∩ 全站 trending（高信号）
- `releases` / `new_repos` — followee 当日发布的 release、新建的仓库
- `raw_counts` — 各事件类型计数

## 工作原理

- `daily-pulse.yml` 每日 UTC 22:00（北京时间早 6 点）运行，也可手动 dispatch。
- followee 事件走 `/users/{u}/events/public` 增量拉取（ETag + last_event_id，状态存 `state/followees.json`）；事件 API 保留 ~90 天/300 条，每日拉取不丢数据。
- 全站 trending 不自己爬，消费 [antonkomarev/github-trending-archive](https://github.com/antonkomarev/github-trending-archive) 的每日归档（含全部语言分列表 + 无语言过滤的首页列表）；归档缺当日数据时回退前两天，仍无则 `trending_available: false`，不导致失败。
- 运行结束后数据与状态文件由 bot 提交回仓库。

## 配置

1. 生成一个 PAT（fine-grained 即可，只需读权限；用于读你的 following 列表——Actions 默认的 `GITHUB_TOKEN` 读不到）。
2. 仓库 Settings → Secrets → Actions 添加 `GH_PAT`。

## 本地开发

```bash
uv sync
GH_PAT=xxx uv run python -m sentinel.run            # 全链路
GH_PAT=xxx uv run python -m sentinel.collectors.followees   # 只跑采集
uv run python -m sentinel.collectors.trending        # 只跑 trending 适配器
GH_USER=<某公开用户> uv run python -m sentinel.collectors.followees  # 无 PAT 冒烟测试
```

## Roadmap

- Star/fork 整理与 LLM 分析报告（独立 workflow 的非契约模块）
- 薄 MCP server（`sentinel-mcp`），供 Agent 宿主以 stdio 拉起查询 pulse 数据
