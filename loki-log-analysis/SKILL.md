---
name: loki-log-analysis
description: Query Loki for error and warning logs from the past N hours, deduplicate by pattern, and generate an anomaly-first markdown report. Normal INFO/DEBUG logs are excluded from analysis.
version: 1.2.0
user-invocable: true
metadata:
  openclaw:
    skillKey: loki-log-analysis
  requires:
    bins:
      - python3
    config:
      - key: LOKI_URL
        description: "Loki HTTP base URL, overridable via --url at runtime (default: http://192.168.88.128:3100)"
        required: false
---

# Loki 日志分析

## 概述

对 Loki 中所有文件日志进行**错误和告警分析**：
- 默认地址：`http://192.168.88.128:3100`，可通过 `--url` 在调用时覆盖
- **在 Loki 层面**使用 LogQL pipeline 过滤，只拉取 ERROR/WARN 级别日志
- **在 Python 层面**按消息模式去重，把 5000+ 行压缩为 Top-100 独特模式（可通过 `--max-patterns` 调整，范围 10-500）
- 报告**异常优先**，无告警则跳过详情章节

---

## 执行步骤

### 第一步：提取用户参数

| 参数 | 默认值 | 用户说法示例 |
|---|---|---|
| Loki 地址 | `http://192.168.88.128:3100` | "loki 是 http://10.0.0.5:3100" |
| 时间范围 | `6` 小时 | "最近 2 小时" / "过去 1 天" |
| 模式 | `errors-only` | "查所有日志"（则加 --all-logs） |

### 第二步：定位辅助脚本

- Workspace：`<workspace>/.qoder/skills/loki-log-analysis/scripts/fetch_loki_logs.py`
- 全局：`~/.openclaw/skills/loki-log-analysis/scripts/fetch_loki_logs.py`

Windows 定位：
```powershell
Get-ChildItem -Recurse -Filter "fetch_loki_logs.py" "$env:USERPROFILE" -ErrorAction SilentlyContinue | Select -First 5 FullName
```

### 第三步：运行脚本

```bash
python3 <skill_dir>/scripts/fetch_loki_logs.py \
  --url {LOKI_URL} \
  --hours {HOURS} \
  --errors-only \
  --max-patterns 50 \
  --output text
```

参数说明：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--url` | `http://192.168.88.128:3100` | **Loki 地址，调用时必须替换** |
| `--hours` | `6` | 查询时间窗口（小时） |
| `--limit` | `5000` | Loki 层每个 stream 最多返回行数 |
| `--errors-only` | 默认开启 | **在 Loki 侧用 LogQL 过滤，只拉 ERROR/WARN** |
| `--all-logs` | 关闭 | 拉取全部日志（不推荐，context 很大） |
| `--max-patterns` | `100` | 每文件最多展示的独特错误模式数（范围 10-500，默认 100） |

### 第四步：处理脚本异常

如脚本不可用，用 curl 手动查询（`{LOKI_URL}` 替换为实际地址）：

```bash
# 连通性检查
curl -s "{LOKI_URL}/ready"

# 手动过滤 ERROR/WARN（近 6 小时）
START=$(python3 -c "import time; print(int((time.time()-21600)*1e9))")
END=$(python3 -c "import time; print(int(time.time()*1e9))")
curl -s -G "{LOKI_URL}/loki/api/v1/query_range" \
  --data-urlencode 'query={filename=~".+"} |~ `(?i)(\[?(error|err|warn|warning)\]?|\berror\b|\bwarn\b)`' \
  --data-urlencode "start=$START" \
  --data-urlencode "end=$END" \
  --data-urlencode "limit=5000"
```

### 第五步：分析并生成报告

> **⚠️ 关键指令：ERROR/WARN 识别规则（必须严格遵守）**
>
> 以下所有格式均代表同一级别，**缺一不可**：
>
> | 级别 | 等价写法（大小写均算） |
> |---|---|
> | 🔴 **ERROR** | `[ERROR]` `[Error]` `[ERR]` `[err]` `ERROR` `Error` `error` `err:` `ERR:` |
> | 🟡 **WARN** | `[WARN]` `[Warning]` `[WARNING]` `[warn]` `WARN` `Warning` `WARNING` `warn:` |
>
> 脚本输出中每行已带有 `[ERROR]` 或 `[WARN]` 标签（LEVEL 列），**以该标签为准**，无论原始日志格式如何。
>
> **上下文大小控制策略：**
> - 脚本已在 Loki 侧过滤，输出只含 ERROR/WARN
> - 脚本已按模式去重，输出为 Top-N 独特模式（含出现次数、首/末时间、样例）
> - 若过滤后仍超过 200 行输出 → **只分析 TOP 100 高频模式**，其余折叠进附录
> - 若同一模式出现 100+ 次 → 视为持续性故障，重点分析

---

## 报告模板（严格按照此格式输出）

```markdown
# Loki 日志分析报告

**分析时间**：{当前时间}
**Loki 地址**：{实际 LOKI_URL}
**分析时间段**：近 {N} 小时（{开始} ~ {结束}）
**日志文件数**：{N} 个
**过滤模式**：仅 ERROR / WARN（已在 Loki 侧过滤）
**过滤后行数**：{N} 条（🔴 ERROR {N} · 🟡 WARN {N}）

---

## 一、执行摘要

> **整体状态**：{健康 ✅ / 需关注 ⚠️ / 异常 ❌}
> **异常**：🔴 ERROR {N} 条 · 🟡 WARN {N} 条

{2-3 句话描述最关键问题；若 ERROR=0 且 WARN=0 则写
"过滤后未发现 ERROR/WARN 日志，系统运行正常"，后续章节二至四全部跳过。}

---

## 二、错误与告警汇总（按文件+频率排序）

> 数据来自脚本 `TOP PATTERNS` 部分。`[N次]` 表示该模式出现次数。

| 级别 | 文件 | 次数 | 首次出现 | 末次出现 | 日志样例 |
|---|---|---|---|---|---|
| 🔴 ERROR | `{file}` | {N} | {time} | {time} | `{example}` |
| 🟡 WARN  | `{file}` | {N} | {time} | {time} | `{example}` |

> 若无任何 ERROR/WARN，写"当前无错误和告警"，**三至四章跳过**。

---

## 三、异常文件详细分析

> **仅分析含 ERROR/WARN 的文件；纯 INFO 文件跳过。**

### ⚠️ {filename}

**概览**：{一句话描述主要问题}

#### 高频错误模式（Top N）

| 模式 | 出现次数 | 首次 | 末次 |
|---|---|---|---|
| `{normalized pattern}` | {N} | {time} | {time} |

> 同一模式出现 100+ 次 → 标注为「**持续性故障**」

#### 关键事件时间线（仅 ERROR/WARN，最多 10 条）

| 时间 | 级别 | 原始日志 |
|---|---|---|
| {timestamp} | 🔴 ERROR | `{line}` |
| {timestamp} | 🟡 WARN  | `{line}` |

#### 根因推断

{2-4 句话，基于错误模式和时间线推断根本原因}

---

（其余含 ERROR/WARN 的文件重复上述结构）

---

## 四、行动建议

> **仅列出与 ERROR/WARN 直接相关的建议；无告警则跳过。**

### 立即处理 🔴
1. {具体步骤，明确文件/服务名称}

### 近期跟进 🟡
1. {建议}

### 长期优化 🟢
1. {建议}

---

## 五、附录

<details>
<summary>展开：所有文件统计（含正常文件）</summary>

| 文件 | 总行数 | ERROR | WARN | 状态 |
|---|---|---|---|---|
| `{file}` | {N} | {N} | {N} | {✅/⚠️/❌} |

</details>

<details>
<summary>展开：低频错误模式（排名 21-50）</summary>

```
{低频模式列表，格式：[N次] pattern}
```

</details>
```

---

## 注意事项

- **ERROR 识别**：脚本在输出的 LEVEL 列已标注 `ERROR` 或 `WARN`，**以此为准**；
  原始日志中的 `[ERROR]` `[ERR]` `Error` `error` `err:` 等均已被脚本识别为 ERROR 级别。
- **WARN 识别**：`[WARN]` `[Warning]` `[WARNING]` `WARN` `Warning` `warn:` 均为 WARN 级别。
- **大日志处理**：脚本输出为去重后的模式摘要（非原始行），若模式仍超 200 条，只在报告主体展示 TOP 20。
- **全部正常时**：一章摘要写明后，二至四章全部省略，只输出附录统计。
- **URL 规则**：报告头和排查命令中的地址为用户实际指定的地址。
- **准确性**：所有数值来自脚本实际输出，禁止估算。
- **语言**：报告全程中文，日志原文保留原格式。
