---
name: loki-log-analysis
description: Query Loki for all file logs from the past N hours and generate a markdown analysis report that prioritizes errors and warnings; normal INFO logs are not reported in detail.
version: 1.1.0
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

当用户要求分析 Loki 日志、查看近期错误或检查日志异常时，执行本 Skill。  
默认连接 `http://192.168.88.128:3100`，**可通过 `--url` 参数在调用时指定任意 Loki 地址**。  
报告**只分析 ERROR / WARN 及以上级别**，正常 INFO/DEBUG 日志不展开报告，仅在附录做数量统计。

---

## 执行步骤

### 第一步：从用户指令中提取参数

读取用户输入，识别以下参数（未指定则用默认值）：

| 参数 | 默认值 | 用户可能的表达方式 |
|---|---|---|
| Loki 地址 | `http://192.168.88.128:3100` | "loki 地址是 http://x.x.x.x:3100" |
| 时间范围 | `6` 小时 | "查看最近 2 小时" / "近 1 天" |

### 第二步：定位辅助脚本

找到本 Skill 目录中的 `fetch_loki_logs.py`：

- Workspace：`<workspace>/.qoder/skills/loki-log-analysis/fetch_loki_logs.py`
- 全局：`~/.openclaw/skills/loki-log-analysis/fetch_loki_logs.py`

Windows 环境定位：
```powershell
Get-ChildItem -Recurse -Filter "fetch_loki_logs.py" -Path "$env:USERPROFILE" -ErrorAction SilentlyContinue | Select-Object -First 5 FullName
```

### 第三步：运行脚本抓取日志

将 `{LOKI_URL}` 和 `{HOURS}` 替换为实际值后执行：

```bash
python3 <skill_dir>/fetch_loki_logs.py --url {LOKI_URL} --hours {HOURS} --limit 2000 --output text
```

参数说明：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--url` | `http://192.168.88.128:3100` | **Loki 地址，用户指定时必须替换** |
| `--hours` | `6` | 抓取近 N 小时 |
| `--limit` | `2000` | 每个日志流最多条数 |
| `--output` | `text` | 输出格式：`text` 或 `json` |

示例（指定地址和时间）：
```bash
python3 <skill_dir>/fetch_loki_logs.py --url http://10.0.0.5:3100 --hours 2 --output text
```

### 第四步：处理脚本异常

如脚本运行失败，将 `{LOKI_URL}` 替换为实际地址后执行：

1. **连通性检查**：
   ```bash
   curl -s "{LOKI_URL}/ready"
   ```
2. **手动标签查询**：
   ```bash
   curl -s "{LOKI_URL}/loki/api/v1/labels"
   ```
3. **手动日志查询**（替代方案）：
   ```bash
   START=$(python3 -c "import time; print(int((time.time()-21600)*1e9))")
   END=$(python3 -c "import time; print(int(time.time()*1e9))")
   curl -s -G "{LOKI_URL}/loki/api/v1/query_range" \
     --data-urlencode 'query={filename=~".+"}' \
     --data-urlencode "start=$START" \
     --data-urlencode "end=$END" \
     --data-urlencode "limit=2000" \
     --data-urlencode "direction=forward"
   ```

如以上均失败，明确告知用户 Loki 地址不可达，建议检查网络或 Loki 服务状态。

### 第五步：分析日志并生成 Markdown 报告

获取脚本输出后，**作为 AI 分析员**进行分析，按以下模板输出报告。  
**核心原则：只分析 ERROR / WARN 及以上级别；INFO/DEBUG 不逐条展开，仅统计数量。**

---

## 报告模板（严格按照此格式输出）

```markdown
# Loki 日志分析报告

**分析时间**：{当前时间}
**Loki 地址**：{实际使用的 LOKI_URL}
**分析时间段**：近 {N} 小时（{开始时间} ~ {结束时间}）
**日志文件数**：{N} 个
**日志总行数**：{N} 条（🔴 ERROR {N} · 🟡 WARN {N} · 其他 {N}）

---

## 一、执行摘要

> **整体状态**：{健康 ✅ / 需关注 ⚠️ / 异常 ❌}
> **异常项**：🔴 ERROR {N} 条 &nbsp; 🟡 WARN {N} 条

{2-3 句话说明最关键的问题；若无 ERROR/WARN 则一句话说明"日志中未发现错误和告警"，
后续章节（二至四）跳过，直接输出附录。}

---

## 二、错误与告警汇总（异常优先）

> **本节仅列出 ERROR / FATAL / WARN 级别日志，INFO 及以下不展示。**

| 级别 | 来源文件 | 出现次数 | 典型日志示例 |
|---|---|---|---|
| 🔴 ERROR | `{file}` | {N} | `{最具代表性的一条}` |
| 🔴 FATAL | `{file}` | {N} | `{example}` |
| 🟡 WARN  | `{file}` | {N} | `{example}` |

> 如无任何 ERROR/WARN，填写「当前时间段日志未发现错误和告警，系统运行正常」，
> **三至四章跳过**，直接输出附录。

---

## 三、异常文件详细分析

> **仅对包含 ERROR 或 WARN 的文件展开，纯 INFO 文件跳过。**

### ⚠️ {filename_with_errors}

**异常概览**：{一句话描述该文件日志中的主要问题}

#### 错误模式（Top 5，按频率排序）

| 错误模式 / 关键词 | 出现次数 | 首次出现 | 末次出现 |
|---|---|---|---|
| `{error pattern}` | {N} | {time} | {time} |

#### 告警模式

| 告警模式 | 出现次数 | 示例 |
|---|---|---|
| `{warn pattern}` | {N} | `{example}` |

#### 关键事件时间线（最多 10 条，仅 ERROR/WARN）

| 时间 | 级别 | 日志内容 |
|---|---|---|
| {timestamp} | 🔴 ERROR | `{log line}` |
| {timestamp} | 🟡 WARN  | `{log line}` |

#### 根因推断

{对该文件错误/告警的可能原因分析，2-4 句话}

---

（其余含 ERROR/WARN 的文件重复上述结构；纯 INFO 文件不输出）

---

## 四、行动建议

> **仅列出与当前 ERROR/WARN 直接相关的建议；无告警则本章跳过。**

### 立即处理（高优先级 🔴）
1. {具体可操作步骤，明确文件/服务名称}

### 近期跟进（中优先级 🟡）
1. {建议}

### 长期优化（低优先级 🟢）
1. {建议}

---

## 五、附录：全量文件统计

<details>
<summary>展开查看所有日志文件统计（含正常文件）</summary>

| 文件路径 / 来源 | 总行数 | ERROR | WARN | INFO/其他 | 状态 |
|---|---|---|---|---|---|
| `{filename}` | {N} | {N} | {N} | {N} | {✅/⚠️/❌} |

</details>

<details>
<summary>展开查看原始 ERROR/WARN 日志（最多 30 条）</summary>

```
{原始日志行，每行格式：[时间] [文件] [级别] 日志内容}
```

</details>
```

---

## 注意事项

- **异常优先原则**：INFO/DEBUG 日志**不逐条展开**；只有 ERROR/WARN 及以上才进入第二、三章分析。
- **全部正常时**：第一章写明"无错误和告警"后，**二至四章全部跳过**，直接输出第五章附录。
- **文件跳过规则**：某文件整个时间段内只有 INFO，跳过该文件的详细分析节。
- **URL 使用规则**：报告头部和排查命令中的地址必须是用户实际指定的地址（不得写死默认值）。
- **无日志时**：明确说明"在指定时间段内未查询到任何日志"，并列出已检查的标签。
- **准确性要求**：所有数值必须来自脚本实际输出，禁止估算或编造。
- **报告语言**：始终以**中文**输出，日志原文保留原始格式。
