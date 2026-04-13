# GitHub Trending Digest

追踪 GitHub Trending 三个周期（daily/weekly/monthly）的热门项目，通过 HTML 正则解析抓取、SQLite 存储，由 Hermes cron job 编排三步隔离反思流水线（Draft → Critique → Refine）生成高质量中文摘要。

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│  Hermes Cron Jobs                                           │
│                                                             │
│  ┌─────────────────────┐    ┌─────────────────────────────┐ │
│  │ Fetch (3x/day)      │    │ Daily Digest (1x/day 22:00) │ │
│  │ 9:00 静默            │    │                             │ │
│  │ 15:00 静默           │    │  script: digest query       │ │
│  │ 21:00 汇报           │    │       ↓ JSON 注入           │ │
│  │                     │    │  Agent 编排 delegate_task   │ │
│  │ script:             │    │       ↓                     │ │
│  │  github_fetch.py    │    │  ┌──────────────────────┐   │ │
│  │       ↓             │    │  │ Subagent 1: Draft    │   │ │
│  │  scrape HTML        │    │  │ (看得到原始数据)       │   │ │
│  │       ↓             │    │  └──────────┬───────────┘   │ │
│  │    SQLite DB        │    │             ↓               │ │
│  └─────────────────────┘    │  ┌──────────────────────┐   │ │
│                             │  │ Subagent 2: Critique │   │ │
│                             │  │ (只看得到初稿，隔离) │   │ │
│                             │  └──────────┬───────────┘   │ │
│                             │             ↓               │ │
│                             │  ┌──────────────────────┐   │ │
│                             │  │ Subagent 3: Refine   │   │ │
│                             │  │ (初稿 + 审稿意见)    │   │ │
│                             │  └──────────┬───────────┘   │ │
│                             │             ↓               │ │
│                             │  ┌──────────────────────┐   │ │
│                             │  │ Step 4: Save Summary │   │ │
│                             │  │ (终稿写入 SQLite DB) │   │ │
│                             │  └──────────┬───────────┘   │ │
│                             │             ↓               │ │
│                             │     最终摘要 → Telegram     │ │
│                             └─────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

## 文件结构

```
~/.hermes/hermes-agent/github-trending-digest/
├── db.py                  # 数据层：SQLite 连接、建表、Focus Profile 默认值
├── github_fetch.py        # 抓取层：HTML 正则解析 GitHub Trending 页面
├── github_digest.py       # 查询层：数据查询、Focus/订阅者管理
├── digest_generate.py     # 摘要层：数据加载 + Focus 过滤 + 三步 Prompt 模板输出
├── data/
│   └── gh_trending.db     # SQLite 数据库
└── README.md

~/.hermes/scripts/
├── gh_trending_fetch.py   # Cron 包装：调用 github_fetch.py fetch --report-hour 21
└── gh_trending_digest.py  # Cron 包装：调用 digest_generate.py query --days 1 --focus ai-ml
```

## 追踪的内容

GitHub Trending 三个时间周期的热门开源项目：

| 周期 | 来源 URL | 说明 |
|------|----------|------|
| daily | github.com/trending?since=daily | 今日热门 |
| weekly | github.com/trending?since=weekly | 本周热门 |
| monthly | github.com/trending?since=monthly | 本月热门 |

通过正则解析 HTML（`<article class="Box-row">`），提取 repo 名、描述、语言、stars、forks、stars_delta、排名。纯标准库，无 BeautifulSoup 依赖。

## 核心文件说明

### db.py

共享数据库模块。建表、初始化默认 Focus Profile、提供 `get_db()` 连接。

**环境变量：** `GH_TRENDING_DB_PATH` 可覆盖默认 DB 路径。

### github_fetch.py

纯 Python 标准库，零外部依赖。通过正则解析 GitHub Trending HTML 页面，存入 SQLite。

**命令：**

| 命令 | 说明 |
|------|------|
| `fetch [--report-hour H]` | 抓取三个周期的 trending 页面，存入 DB。指定 H 时只在该小时输出完整报告 |
| `stats [天数]` | 统计信息（按周期、按语言） |

**特性：**
- 三个周期（daily/weekly/monthly）顺序抓取，间隔 1 秒 rate limit
- `full_name` 自动去重（已有 repo 更新描述和语言）
- `--report-hour` 支持静默抓取（非报告时间只存数据不输出）

### github_digest.py

数据查询 + Focus Profile 管理 + 订阅者管理。输出 focus_rules 但不自身过滤关键词。

**命令：**

| 命令 | 说明 |
|------|------|
| `query [天数] [--period X] [--language Y] [--focus Z]` | 查询 trending 数据，按周期分组输出 JSON |
| `save-summary [period] [focus]` | 从 stdin 保存摘要到 DB |
| `focus-profiles` | 列出所有 Focus Profile |
| `add-focus <名> <JSON>` | 添加自定义 Focus Profile |
| `subscribers` | 列出订阅者 |
| `add-subscriber --email <email> [--name <name>] [--focus <focus>]` | 添加订阅者 |
| `remove-subscriber <email>` | 删除订阅者 |
| `toggle-subscriber <email>` | 启用/暂停订阅者 |

### digest_generate.py

数据加载 + Focus 过滤 + 三步 Prompt 模板输出。按 focus languages 和 keywords 实际过滤项目。不调用 LLM，LLM 调用由 Hermes cron agent 通过 delegate_task 完成。

**命令：**

| 命令 | 说明 |
|------|------|
| `query [--days 1] [--focus ai-ml]` | 输出 trending 数据 + 三步 Prompt 模板 JSON |
| `save-summary [--days 1] [--focus default]` | 从 stdin 保存摘要到 DB |
| `stats` | 简要统计（总 repo、总 entries、总 summaries） |

**query 输出 JSON 结构：**
```json
{
  "meta": { "date", "days", "focus", "focus_instructions", "total_repos", "focused_repos", "daily_count", "weekly_count" },
  "repos": "过滤后的 repo 列表（flat list，限 top_n）",
  "streaks": "连续上榜项目（近 7 天 daily 出现 >1 天）",
  "language_distribution": "语言分布统计",
  "prompts": {
    "draft": "完整的初稿 Prompt（trending 数据已嵌入）",
    "critique_template": "审稿模板（{draft} 占位符）",
    "refine_template": "精修模板（{draft} + {critique} 占位符）"
  }
}
```

## 三步隔离反思设计

核心思想：审稿人看不到原始数据，只能评估摘要质量。

| 步骤 | Subagent | 输入 | 输出 | 隔离 |
|------|----------|------|------|------|
| Draft | #1 | 原始 trending 数据 + 格式指令 | 初稿 | 看得到原始数据 |
| Critique | #2 | 只有初稿 | 审稿意见 + A/B/C 评分 | 看不到原始数据 |
| Refine | #3 | 初稿 + 审稿意见 | 终稿 | 看不到原始数据 |

每个 subagent 通过 Hermes `delegate_task` 创建，天然上下文隔离。

## Focus Profiles

控制摘要关注哪类项目。digest_generate.py 按 languages 和 keywords 实际过滤。

| Profile | 语言过滤 | 关键词 | top_n | 说明 |
|---------|---------|--------|-------|------|
| default | 全部 | 无 | 15 | 均衡关注所有语言 |
| ai-ml | Python, Jupyter Notebook | ai, ml, llm, agent, model, neural, transformer, diffusion, rag, embedding, fine-tune, training, inference | 15 | AI/ML 项目 |
| rust-go | Rust, Go | 无 | 15 | 系统编程和基础设施 |
| frontend | TypeScript, JavaScript | react, vue, svelte, next, css, ui, component, design | 15 | 前端框架和 UI 库 |

自定义示例：
```bash
python3 github_digest.py add-focus myprofile '{
  "languages": ["Python"],
  "keywords": ["mcp", "tool-use"],
  "instructions": "关注 MCP 协议和工具调用相关项目",
  "top_n": 10
}'
```

## 数据库结构

SQLite（`data/gh_trending.db`），5 张表：

| 表 | 说明 |
|----|------|
| repos | 所有 repo，按 full_name 去重（full_name UNIQUE, description, language, url, first_seen） |
| trending_entries | trending 记录（repo, period, rank, stars, stars_delta, forks, fetched_at） |
| summaries | 生成的摘要历史（date, period, focus, content, created_at） |
| focus_profiles | Focus 配置（name UNIQUE, description, rules, created_at） |
| subscribers | 订阅者（name, email UNIQUE, focus, enabled, created_at） |

## Cron Jobs

| Job | 时间 (PST) | 说明 |
|-----|-----------|------|
| GitHub Trending Fetch | 9:00, 15:00, 21:00 | 抓取三周期 trending，21 点发汇报 |
| GitHub Trending Digest | 22:00 | 三步反思生成 ai-ml 摘要，保存到 DB，发送到 Telegram |

## 手动使用

```bash
cd ~/.hermes/hermes-agent/github-trending-digest

# 抓取最新 trending
python3 github_fetch.py fetch

# 查看统计
python3 github_fetch.py stats 7
python3 digest_generate.py stats

# 查询 AI/ML 相关 trending
python3 digest_generate.py query --days 1 --focus ai-ml

# 查询指定语言
python3 github_digest.py query 3 --language Python

# 列出 Focus Profile
python3 github_digest.py focus-profiles

# 列出订阅者
python3 github_digest.py subscribers
```

## 迁移说明

从 OpenClaw workspace 迁移而来。主要改动：

- 纯 HTML 正则解析，无 BeautifulSoup 依赖，零外部包
- `digest_generate.py` 去掉了 OpenClaw Gateway API 调用，改为输出 JSON + Prompt 模板，LLM 调用由 Hermes delegate_task 完成
- Cron 脚本必须是 .py（Hermes scheduler 固定用 Python 解释器执行）
- Cron 脚本必须放在 `~/.hermes/scripts/`（路径校验限制）

## 已知限制

- GitHub 可能改变 HTML 结构导致正则解析失败
- 三步 delegate_task 串行执行，生成摘要需要几分钟
- github_digest.py query 输出 focus_rules 但不按关键词过滤，实际过滤在 digest_generate.py
- 三个周期顺序抓取有 1s 间隔，总抓取时间约 3-5 秒
