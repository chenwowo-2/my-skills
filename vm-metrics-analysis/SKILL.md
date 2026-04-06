---
name: vm-metrics-analysis
description: Query VictoriaMetrics for node metrics (CPU, memory, disk, network, load) and optionally OpenStack, Ceph, Keepalived metrics, then generate a comprehensive markdown analysis report with health scoring and actionable recommendations.
version: 1.0.0
user-invocable: true
metadata:
  openclaw:
    skillKey: vm-metrics-analysis
  requires:
    bins:
      - python3
    config:
      - key: VM_URL
        description: "VictoriaMetrics HTTP base URL (default: http://192.168.88.128:8428)"
        required: false
---

# VictoriaMetrics 指标分析

## 概述

当用户要求查看节点指标、分析系统资源、检查 OpenStack/Ceph/Keepalived 状态时，执行本 Skill。  
默认连接 `http://192.168.88.128:8428`，**可通过 `--url` 参数在调用时指定任意 VictoriaMetrics 地址**。  
通过 Prometheus 兼容 API 采集 **node、openstack、ceph、keepalived** 等指标，并以 **Markdown 格式**输出分析报告。

---

## 执行步骤

### 第一步：从用户指令中提取参数

读取用户输入，识别以下参数（未指定则用默认值）：

| 参数 | 默认值 | 用户可能的表达方式 |
|---|---|---|
| VM 地址 | `http://192.168.88.128:8428` | "vm 地址是 http://x.x.x.x:8428" |
| 趋势时间窗口 | `1` 小时 | "查看最近 6 小时趋势" |

### 第二步：定位辅助脚本

找到本 Skill 目录中的 `fetch_vm_metrics.py`：

- Workspace：`<workspace>/.qoder/skills/vm-metrics-analysis/fetch_vm_metrics.py`
- 全局：`~/.openclaw/skills/vm-metrics-analysis/fetch_vm_metrics.py`

Windows 环境定位命令：
```powershell
Get-ChildItem -Recurse -Filter "fetch_vm_metrics.py" -Path "$env:USERPROFILE" -ErrorAction SilentlyContinue | Select-Object -First 5 FullName
```

### 第三步：运行脚本采集指标

将 `{VM_URL}` 替换为实际地址后执行：

```bash
python3 <skill_dir>/fetch_vm_metrics.py --url {VM_URL} --range-hours 1 --step 60 --output text
```

参数说明：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--url` | `http://192.168.88.128:8428` | **VM 地址，用户指定时必须替换** |
| `--range-hours` | `1` | 趋势查询时间窗口（小时） |
| `--step` | `60` | 趋势查询步长（秒） |
| `--output` | `text` | 输出格式：`text` 或 `json` |
| `--category` | `auto` | 指定采集类别：`node,openstack,ceph,keepalived` 或 `auto`（自动探测） |

示例（指定地址和较长趋势窗口）：
```bash
python3 <skill_dir>/fetch_vm_metrics.py --url http://10.0.0.5:8428 --range-hours 6 --output text
```

> 脚本会自动探测 VictoriaMetrics 中存在的指标类别（node/openstack/ceph/keepalived），无需手动指定。

### 第四步：处理脚本异常

如脚本无法运行或 VictoriaMetrics 不可达，将 `{VM_URL}` 替换为实际地址后执行：

1. **连通性检查**：
   ```bash
   curl -s "{VM_URL}/health"
   ```
2. **手动查询所有 metric 名称**：
   ```bash
   curl -s "{VM_URL}/api/v1/label/__name__/values"
   ```
3. **手动查询节点实例列表**：
   ```bash
   curl -s "{VM_URL}/api/v1/label/instance/values"
   ```
4. **手动查询单个指标**（示例：当前 CPU 使用率）：
   ```bash
   curl -s -G "{VM_URL}/api/v1/query" \
     --data-urlencode 'query=100 - avg by(instance)(irate(node_cpu_seconds_total{mode="idle"}[5m])) * 100'
   ```

如以上均失败，明确告知用户 VictoriaMetrics 不可达，建议检查服务状态或网络。

### 第五步：分析指标数据并生成 Markdown 报告

获取脚本输出后，**作为专业 SRE 工程师**对指标进行系统性分析，按以下模板输出报告。

---

## 报告模板（严格按照此格式输出）

```markdown
# VictoriaMetrics 指标分析报告

**分析时间**：{当前时间}  
**数据源**：http://192.168.88.128:8428  
**覆盖类别**：{node / keepalived / ceph / openstack（仅列出已采集的）}  
**节点数量**：{N} 个实例  

---

## 一、执行摘要

> **整体健康状态**：{健康 ✅ / 需关注 ⚠️ / 异常 ❌}  
> **综合评分**：{X}/10  
> **异常项数**：🔴 紧急 {N} 项 &nbsp; 🟡 警告 {N} 项 &nbsp; 🟢 关注 {N} 项

{2-3 句话描述当前最关键的问题；若全部正常则一句话说明即可}

---

## 二、告警汇总（异常优先）

> **本节为报告核心，仅列出超出阈值或存在风险的指标项，正常项不展示。**

| 级别 | 组件 | 实例 | 指标 | 当前值 | 阈值 | 建议操作 |
|---|---|---|---|---|---|---|
| 🔴 紧急 | {node/ceph/...} | `{instance}` | {CPU/内存/磁盘...} | {X%} | >{阈值} | {建议} |
| 🟡 警告 | {node/ceph/...} | `{instance}` | {指标名} | {X%} | >{阈值} | {建议} |
| 🟢 关注 | {node/ceph/...} | `{instance}` | {指标名} | {X%} | 趋势上升 | {建议} |

> 如无任何告警，此处填写：**「当前所有节点和组件运行正常，无需干预」**，后续章节（三至五）全部跳过，直接输出第六章附录。

---

## 三、异常节点详细分析

> **仅对告警汇总中出现的节点展开分析，未出现告警的节点跳过。**

### ⚠️ {instance_with_issue}

**异常概览**：{一句话描述该节点存在的主要问题}

| 资源 | 当前值 | 均值 | 峰值 | 状态 | 说明 |
|---|---|---|---|---|---|
| CPU | {X%} | {X%} | {X%} | {✅/⚠️/❌} | 仅异常项标注原因 |
| 内存 | {X%}（可用 {X GB}） | - | - | {✅/⚠️/❌} | |
| 磁盘 / | {X%}（可用 {X GB}） | - | - | {✅/⚠️/❌} | |
| 磁盘 I/O | {X%} | - | - | {✅/⚠️/❌} | |
| 网络 RX/TX | {X MB/s} / {X MB/s} | - | - | {✅/⚠️/❌} | |
| Load 1/5/15 | {X}/{X}/{X} | - | - | {✅/⚠️/❌} | |

> 正常指标（✅）行可省略不写，仅保留 ⚠️ 和 ❌ 的行。

**根因推断**：{对该节点异常的可能原因分析}

---

（其余有告警的节点重复上述结构；无告警节点不输出）

---

## 四、异常组件状态

> **以下各小节仅在该组件存在异常时输出，全部正常则整章跳过。**

### 4.1 Keepalived（如有异常）

- **异常描述**：{具体说明，如：检测到多个 MASTER 实例，存在脑裂风险}
- **受影响实例**：`{instance}`，VRRP `{name}`，当前角色 `{MASTER/BACKUP}`
- **建议**：{处理步骤}

> 若 Keepalived 状态正常（单 MASTER、BACKUP 正常接收广播），跳过本节。

### 4.2 Ceph（如有异常）

- **集群健康**：{HEALTH_WARN / HEALTH_ERR，说明具体警告内容}
- **OSD 异常**：{如：3 个 OSD 处于 down 状态，影响数据可用性}
- **PG 异常**：{如：12 个 PG 处于 degraded 状态}
- **容量预警**：{如：已用 87%，预计 N 天后达到 90% 阈值}
- **建议**：{处理步骤}

> 若 Ceph HEALTH_OK 且容量 < 80%，跳过本节。

### 4.3 OpenStack（如有异常）

- **服务下线**：{如：Keystone up=0，认证服务不可用}
- **资源异常**：{如：Nova 实例数骤降，可能发生批量故障}
- **建议**：{处理步骤}

> 若 OpenStack 所有服务 up 且无异常变化，跳过本节。

---

## 五、行动建议

> **仅列出与当前告警直接相关的建议，无告警则本章跳过。**

### 立即处理（高优先级 🔴）
1. {具体可操作步骤，明确节点/服务名称}

### 近期跟进（中优先级 🟡）
1. {建议}

### 长期优化（低优先级 🟢）
1. {建议}

---

## 六、附录：全量节点指标一览

<details>
<summary>展开查看所有节点完整指标（含正常节点）</summary>

| 实例 | 在线时长 | CPU核 | CPU(当前/均/峰) | 内存使用 | 磁盘/ | Load 1/5/15 | 状态 |
|---|---|---|---|---|---|---|---|
| `{instance}` | {Xd Xh} | {N} | {X%}/{X%}/{X%} | {X%} | {X%} | {X}/{X}/{X} | {✅/⚠️/❌} |

</details>

<details>
<summary>展开查看脚本原始输出</summary>

```
{脚本输出的完整原始文本}
```

</details>
```

---

## 注意事项

- **异常优先原则**：正常运行的节点、组件、指标项**不展开描述**；只有存在告警（⚠️/❌）的内容才进入第三、四、五章。
- **全部正常时**：第二章告警汇总写明“无告警”后，**第三至五章全部跳过**，直接输出第六章附录。
- **章节裁剪**：Keepalived/Ceph/OpenStack 各组件只在有异常时输出对应小节，否则整节省略。
- **节点表格行省略**：第三章节点详情表中，✅ 的行可不写，只输出 ⚠️ 和 ❌ 的行。
- **告警阈值参考**：CPU > 85% 为⚠️，> 95% 为🔴；内存可用 < 20% 为⚠️，< 10% 为🔴；磁盘使用 > 80% 为⚠️，> 90% 为🔴；Load1 持续 > CPU 核数 × 0.8 为⚠️。
- **未采集到某类别**：无需单独说明，直接忽略该章节。
- **指标缺失**：若某指标为 `N/A`，不推测数值，跳过该项。
- **报告语言**：始终以**中文**输出，指标名称和实例名保留英文原文。
- **准确性要求**：所有数值必须来自脚本实际输出，禁止估算或编造。
