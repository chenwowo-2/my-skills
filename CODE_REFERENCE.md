# OpenClaw 可观测性 Skill — 完整代码说明

> 包含两个 Skill 的所有脚本详解：Loki 日志分析 + VictoriaMetrics 指标分析。

---

## 目录

- [一、整体架构](#一整体架构)
- [二、Loki 日志分析脚本详解](#二loki-日志分析脚本详解)
- [三、VictoriaMetrics 指标分析脚本详解](#三victoriametrics-指标分析脚本详解)
- [四、通用设计模式](#四通用设计模式)
- [五、错误处理策略](#五错误处理策略)
- [六、命令行速查](#六命令行速查)

---

## 一、整体架构

### 1.1 目录结构

```
.qoder/skills/
├── loki-log-analysis/
│   ├── SKILL.md                          ← OpenClaw 触发词定义 + 指令
│   └── scripts/
│       └── fetch_loki_logs.py            ← Loki 日志采集脚本（346 行）
│
└── vm-metrics-analysis/
    ├── SKILL.md                          ← OpenClaw 触发词定义 + 指令
    ├── scripts/
    │   └── fetch_vm_metrics.py           ← VictoriaMetrics 指标采集脚本（703 行）
    └── references/
        ├── keepalived.txt                ← keepalived_exporter 原始指标样本
        ├── rabbitmq.txt                  ← RabbitMQ 内置 prometheus 样本
        ├── ceph.txt                      ← ceph-mgr prometheus 样本
        ├── openstack.txt                 ← openstack-exporter 样本
        └── mysql.txt                     ← MySQL exporter 样本（备用）
```

### 1.2 数据流向

```
用户指令 → OpenClaw 匹配 SKILL.md description → 读取脚本 → 执行采集
                                                          ↓
                                              Loki API / VictoriaMetrics API
                                                          ↓
                                              原始数据（日志行 / 时序数据）
                                                          ↓
                                              过滤 → 去重 → 分组 → 格式化
                                                          ↓
                                              text 输出 → 大模型分析 → Markdown 报告
                                              json 输出 → 程序解析
```

### 1.3 零依赖设计

两个脚本**仅使用 Python 标准库**，无需 `pip install`：

| 模块 | 用途 |
|---|---|
| `urllib.request` | HTTP 请求（替代 `requests`） |
| `urllib.parse` | URL 编码（替代 `urllib` 拼接） |
| `urllib.error` | HTTP 异常处理 |
| `json` | 解析/序列化 JSON |
| `argparse` | 命令行参数解析 |
| `re` | 正则表达式匹配 |
| `time` | 时间戳计算 |
| `datetime` | 时间格式化 |
| `collections.Counter` | 计数统计 |
| `collections.defaultdict` | 自动初始化字典 |
| `sys` | stderr 日志输出 |

---

## 二、Loki 日志分析脚本详解

### 2.1 文件信息

- **路径**: `skills/loki-log-analysis/scripts/fetch_loki_logs.py`
- **行数**: 346 行
- **功能**: 从 Loki 查询所有文件日志 → 过滤 ERROR/WARN → 模式去重 → 输出摘要报告

### 2.2 核心常量

```python
DEFAULT_LOKI_URL = "http://192.168.88.128:3100"

# LogQL pipeline 过滤器 — 在 Loki 服务端过滤
LOKI_ERROR_FILTER = r'|~ `(?i)(\[(error|err|warn|warning)\]|\b(error|err|warning|warn)\b)`'
```

**LogQL 过滤器拆解**:

| 部分 | 含义 |
|---|---|
| `\|~` | LogQL pipeline 匹配操作符（正则匹配） |
| `` ` `` | 反引号包裹正则（LogQL 语法） |
| `(?i)` | 不区分大小写 |
| `\[(error\|err\|warn\|warning)\]` | 匹配 `[ERROR]`、`[ERR]`、`[WARN]`、`[WARNING]` |
| `\b(error\|err\|warning\|warn)\b` | 匹配单词边界的 `error`、`err`、`warning`、`warn` |

### 2.3 日志级别分类正则

```python
_LEVEL_RE = re.compile(
    r'(?i)\[(ERROR|ERR|CRITICAL|FATAL)\]'  # 方括号形式
    r'|\b(ERROR|CRITICAL|FATAL)\b'          # 单词边界
    r'|\b(err)\s*[:=]',                    # err: 或 err= 形式
    re.IGNORECASE
)
_WARN_RE = re.compile(
    r'(?i)\[(WARN|WARNING)\]|\b(WARN|WARNING)\b',
    re.IGNORECASE
)
```

**分类优先级**: 先匹配 ERROR（含 CRITICAL/FATAL），再匹配 WARN，其余为 OTHER。

### 2.4 模式归一化正则（去重核心）

```python
_NORM_TS  = re.compile(r'\d{4}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:\d{2})?')
_NORM_IP  = re.compile(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?::\d+)?\b')
_NORM_HEX = re.compile(r'\b[0-9a-fA-F]{8,}\b')
_NORM_NUM = re.compile(r'\b\d[\d.,]*\b')
_NORM_UUID= re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.IGNORECASE)
```

**归一化函数**:

```python
def normalize_line(line: str) -> str:
    s = _NORM_TS.sub('<TS>', line)   # 2024-01-15 10:30:45 → <TS>
    s = _NORM_UUID.sub('<UUID>', s)  # 550e8400-e29b-41d4 → <UUID>
    s = _NORM_IP.sub('<IP>', s)      # 192.168.1.100:8080 → <IP>
    s = _NORM_HEX.sub('<HEX>', s)    # 0xDEADBEEF → <HEX>
    s = _NORM_NUM.sub('<N>', s)      # 12345 → <N>
    return s[:160].strip()           # 截断 160 字符
```

**效果示例**:

| 原始日志 | 归一化后 |
|---|---|
| `2024-01-15 10:30:45 [ERROR] Connection timeout to 192.168.1.100:3306 after 5000ms` | `[ERROR] Connection timeout to <IP> after <N>ms` |
| `2024-01-15 10:30:46 [ERROR] Connection timeout to 192.168.1.101:3306 after 3000ms` | `[ERROR] Connection timeout to <IP> after <N>ms` |

→ 两条不同日志归一化为同一模式，**合并计数**。

### 2.5 函数详解

#### 2.5.1 HTTP 请求函数 `loki_get()`

```python
def loki_get(base_url: str, path: str, params: dict = None) -> dict
```

- 拼接 URL + 自动 urlencode 参数
- 设置 `Accept: application/json` 头
- 30 秒超时
- 捕获 `HTTPError`（输出错误 body 前 300 字符）和通用异常
- 失败返回空字典 `{}`，不抛出异常（让上层函数安全处理）

#### 2.5.2 发现函数族

| 函数 | Loki API | 用途 |
|---|---|---|
| `get_all_labels(base_url)` | `/loki/api/v1/labels` | 获取所有标签名（如 `filename`, `job`） |
| `get_label_values(base_url, label)` | `/loki/api/v1/label/{name}/values` | 获取某标签的所有取值 |
| `get_all_series(base_url, start_ns, end_ns)` | `/loki/api/v1/series` | 获取时间段内所有日志流 |

#### 2.5.3 选择器构建 `build_selector_from_labels()`

```python
def build_selector_from_labels(labels: list) -> str
```

按优先级生成 LogQL 选择器：
1. 有 `filename` 标签 → `'{filename=~".+"}'`（匹配所有文件）
2. 有 `job` 标签 → `'{job=~".+"}'`（匹配所有 job）
3. 有任意标签 → 取第一个，`'{标签名=~".+"}'`
4. 都没有 → `'{__name__=~".+"}'`（兜底）

#### 2.5.4 日志查询 `query_logs()`

```python
def query_logs(base_url: str, log_selector: str, start_ns: int, end_ns: int,
               limit: int = 2000, errors_only: bool = True) -> list
```

- 当 `errors_only=True` 时，在选择器后追加 `LOKI_ERROR_FILTER`
- 调用 `/loki/api/v1/query_range`（范围查询）
- 方向：`forward`（时间正序）
- 时间戳：纳秒级 Unix 时间戳
- 返回每条日志的完整结构：

```python
{
    "timestamp_ns": 1705312245000000000,
    "timestamp": "2024-01-15 10:30:45 UTC",
    "labels": {"filename": "/var/log/app.log"},
    "line": "2024-01-15 10:30:45 [ERROR] Connection timeout...",
    "level": "ERROR"  # 由 classify_level() 分类
}
```

#### 2.5.5 去重分组 `deduplicate_entries()`

```python
def deduplicate_entries(entries: list, max_patterns: int = 100) -> dict
```

**算法流程**:

```
1. 按 filename/job 将日志分组到 by_file 字典
2. 对每个文件：
   a. 遍历所有日志行
   b. 对每行做 normalize_line() 得到模式
   c. Counter 统计模式出现次数
   d. 记录每种模式的首次/末次时间
   e. 记录每种模式的一条样例
   f. 统计 ERROR/WARN 数量
3. 对每个文件取 Top-N 模式（按频率排序）
4. 返回结果字典
```

**返回结构**:

```python
{
    "/var/log/app.log": {
        "total_lines": 5000,
        "level_counts": {"ERROR": 4500, "WARN": 500},
        "top_patterns": [
            {
                "pattern": "[ERROR] Connection timeout to <IP> after <N>ms",
                "count": 3200,
                "first_seen": "2024-01-15 10:00:01 UTC",
                "last_seen": "2024-01-15 15:59:59 UTC",
                "example": "2024-01-15 10:00:01 [ERROR] Connection timeout to 192.168.1.100:3306 after 5000ms"
            },
            ...
        ],
        "raw_sample": [  # 前 30 条原始日志
            {"timestamp": "...", "level": "ERROR", "line": "..."},
            ...
        ]
    }
}
```

#### 2.5.6 文本报告 `build_text_report()`

输出格式化的文本报告，包含：
- 摘要头（地址、时间、模式、行数、ERROR/WARN 统计）
- 每个文件的状态（✅/⚠️/❌）+ Top 模式列表
- 前 30 条 ERROR/WARN 时间线

### 2.6 main() 执行流程

```
1. 解析命令行参数
2. 计算时间范围（now_ns, start_ns）
3. 获取所有标签 → 构建选择器 → 获取日志流数量
4. 获取所有 filename 列表
5. 如果有 filename：
   - 逐个文件查询（每个文件 limit 条）
   否则：
   - 用通用选择器一次性查询
6. 所有日志按时间排序
7. 调用 deduplicate_entries() 去重分组
8. 根据 --output 输出 JSON 或文本
```

### 2.7 命令行参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `--url` | string | `http://192.168.88.128:3100` | Loki 地址 |
| `--hours` | float | `6.0` | 查询时间窗口（小时） |
| `--limit` | int | `5000` | 每个 stream 最大返回行数 |
| `--errors-only` | flag | True | 在 Loki 侧只拉 ERROR/WARN |
| `--all-logs` | flag | False | 关闭错误过滤，拉取全量 |
| `--max-patterns` | int | `50` | 每个文件展示的独特模式数 |
| `--output` | choice | `text` | 输出格式：`text` / `json` |

---

## 三、VictoriaMetrics 指标分析脚本详解

### 3.1 文件信息

- **路径**: `skills/vm-metrics-analysis/scripts/fetch_vm_metrics.py`
- **行数**: 703 行
- **功能**: 从 VictoriaMetrics 查询 5 类指标 → 按实例分组 → 输出格式化报告

### 3.2 支持的指标类别

| 类别 | 前缀 | 数据源 | 指标数 | 采集方式 |
|---|---|---|---|---|
| **node** | `node_` | node_exporter | 14 个即时 + 4 个趋势 | Instant + Range |
| **keepalived** | `keepalived_` | keepalived_exporter | 12 个 | Instant |
| **ceph** | `ceph_` | ceph-mgr prometheus module | 17 个 | Instant |
| **openstack** | `openstack_` | openstack-exporter | 18 个 | Instant |
| **rabbitmq** | `rabbitmq_` | RabbitMQ built-in prometheus | 21 个 | Instant |

### 3.3 函数详解

#### 3.3.1 HTTP 请求函数 `vm_get()`

与 Loki 的 `loki_get()` 结构一致，唯一区别是目标为 VictoriaMetrics 的 Prometheus 兼容 API。

#### 3.3.2 PromQL 查询函数

| 函数 | API 端点 | 用途 |
|---|---|---|
| `instant_query(base_url, expr)` | `/api/v1/query` | 即时查询，获取当前值 |
| `range_query(base_url, expr, start, end, step)` | `/api/v1/query_range` | 范围查询，获取时间序列 |

#### 3.3.3 标签与指标发现

| 函数 | API | 用途 |
|---|---|---|
| `get_label_values(base_url, label)` | `/api/v1/label/{name}/values` | 获取某标签的取值列表 |
| `get_metric_names(base_url)` | 调用 get_label_values("__name__") | 获取所有指标名（用于自动探测） |

#### 3.3.4 格式化辅助函数

| 函数 | 功能 | 示例 |
|---|---|---|
| `fmt_bytes(b)` | 字节单位转换 | `1536.0 MB`, `2.3 GB` |
| `fmt_pct(v)` | 百分比格式化 | `85.3%` |
| `scalar(result, instance)` | 从 instant 查询结果中提取单值 | 返回 `float` 或 `None` |
| `series_avg(values)` | 计算时间序列平均值 | `[[ts1, v1], [ts2, v2]] → avg` |
| `series_max(values)` | 计算时间序列最大值 | `[[ts1, v1], [ts2, v2]] → max` |

#### 3.3.5 Node 指标采集

**即时查询指标（14 个）**:

| key | PromQL | 含义 |
|---|---|---|
| `cpu_usage_pct` | `100 - avg by(instance)(irate(node_cpu_seconds_total{mode="idle"}[5m])) * 100` | CPU 使用率（百分比） |
| `mem_used_pct` | `(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100` | 内存使用率 |
| `mem_total_bytes` | `node_memory_MemTotal_bytes` | 内存总量 |
| `mem_avail_bytes` | `node_memory_MemAvailable_bytes` | 内存可用量 |
| `load1` | `node_load1` | 1 分钟负载 |
| `load5` | `node_load5` | 5 分钟负载 |
| `load15` | `node_load15` | 15 分钟负载 |
| `uptime_seconds` | `time() - node_boot_time_seconds` | 运行时长（秒） |
| `cpu_count` | `count by(instance)(node_cpu_seconds_total{mode="idle"})` | CPU 核心数 |
| `disk_root_used_pct` | `(1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}) * 100` | 根分区使用率 |
| `disk_root_total_bytes` | `node_filesystem_size_bytes{mountpoint="/"}` | 根分区总量 |
| `disk_root_avail_bytes` | `node_filesystem_avail_bytes{mountpoint="/"}` | 根分区可用量 |
| `net_rx_bps` | `sum by(instance)(irate(node_network_receive_bytes_total{device!="lo"}[5m]))` | 网络接收速率 |
| `net_tx_bps` | `sum by(instance)(irate(node_network_transmit_bytes_total{device!="lo"}[5m]))` | 网络发送速率 |
| `disk_io_util` | `avg by(instance)(irate(node_disk_io_time_seconds_total[5m])) * 100` | 磁盘 I/O 利用率 |

**范围查询指标（4 个）**:

用于计算趋势（平均值 + 峰值）：
- `cpu_usage_pct` → `cpu_usage_pct_avg`, `cpu_usage_pct_max`
- `mem_used_pct` → `mem_used_pct_avg`, `mem_used_pct_max`
- `load1` → `load1_avg`, `load1_max`
- `disk_io_util` → `disk_io_util_avg`, `disk_io_util_max`

**采集逻辑**:

```
1. 获取所有 instance 列表
2. 对所有 NODE_QUERIES 做 instant 查询
3. 对所有 NODE_RANGE_QUERIES 做 range 查询
4. 遍历每个 instance：
   a. 提取即时值
   b. 匹配时间序列计算 avg/max
5. 返回 {instances: [...], nodes: {inst: {...}, ...}}
```

#### 3.3.6 Keepalived 指标

| key | PromQL | 含义 |
|---|---|---|
| `keepalived_up` | `keepalived_up` | 服务是否存活（1=up） |
| `vrrp_state` | `keepalived_vrrp_state` | VRRP 状态（1=MASTER, 2=BACKUP, 3=FAULT） |
| `become_master` | `keepalived_become_master_total` | 成为 MASTER 次数 |
| `release_master` | `keepalived_release_master_total` | 释放 MASTER 次数 |
| `advert_sent_rate` | `irate(keepalived_advertisements_sent_total[5m])` | 广播发送速率 |
| `advert_rcvd_rate` | `irate(keepalived_advertisements_received_total[5m])` | 广播接收速率 |
| `auth_failure` | `keepalived_authentication_failure_total` | 认证失败次数（非 0 = 异常） |
| `auth_mismatch` | `keepalived_authentication_mismatch_total` | 认证不匹配次数 |
| `script_state` | `keepalived_script_state` | 健康检查脚本状态（0=OK, 1=FAIL） |
| `script_status` | `keepalived_script_status` | 健康检查脚本状态（0=disabled, 1=enabled） |
| `ttl_errors` | `keepalived_ip_ttl_errors_total` | TTL 错误 |
| `pkt_length_errors` | `keepalived_packet_length_errors_total` | 包长度错误 |

**VRRP 状态映射**:

| 值 | 含义 | 风险 |
|---|---|---|
| 1 | MASTER | 正常（单 MASTER） |
| 2 | BACKUP | 正常（多 BACKUP） |
| 3 | FAULT | 异常，需要关注 |

**异常检测**: `auth_failure > 0` 表示存在脑裂风险或密码不匹配。

#### 3.3.7 Ceph 指标

| key | PromQL | 含义 |
|---|---|---|
| `health_status` | `ceph_health_status` | 集群健康（0=OK, 1=WARN, 2=ERR） |
| `mon_quorum` | `ceph_mon_quorum_status` | Monitor 仲裁状态 |
| `osd_up` | `sum(ceph_osd_up)` | 在线 OSD 数量 |
| `osd_in` | `sum(ceph_osd_in)` | 加入集群的 OSD 数量 |
| `osd_flag_noout` | `ceph_osd_flag_noout` | noout 标志（维护模式） |
| `osd_flag_noup` | `ceph_osd_flag_noup` | noup 标志 |
| `osd_flag_nodown` | `ceph_osd_flag_nodown` | nodown 标志 |
| `osd_flag_norecover` | `ceph_osd_flag_norecover` | norecover 标志 |
| `pg_total` | `sum(ceph_pg_total)` | PG 总数 |
| `pg_active` | `sum(ceph_pg_active)` | 活跃 PG 数 |
| `pg_clean` | `sum(ceph_pg_clean)` | 干净 PG 数 |
| `pg_degraded` | `sum(ceph_pg_degraded)` | 降级 PG 数（⚠️ 非 0 需关注） |
| `pg_stale` | `sum(ceph_pg_stale)` | 过期 PG 数（⚠️ 非 0 需关注） |
| `pool_avail_bytes` | `sum(ceph_pool_max_avail)` | 存储池可用量 |
| `pool_used_bytes` | `sum(ceph_pool_used_bytes)` | 存储池已用 |
| `cluster_capacity_bytes` | `ceph_cluster_capacity_bytes` | 集群总容量 |
| `cluster_used_bytes` | `ceph_cluster_used_bytes` | 集群已用 |
| `pool_read_bps` | `sum(irate(ceph_pool_rd_bytes[5m]))` | 池读取速率 |
| `pool_write_bps` | `sum(irate(ceph_pool_wr_bytes[5m]))` | 池写入速率 |
| `pool_read_ops` | `sum(irate(ceph_pool_rd[5m]))` | 池读 IOPS |
| `pool_write_ops` | `sum(irate(ceph_pool_wr[5m]))` | 池写 IOPS |

#### 3.3.8 OpenStack 指标

| 类别 | 指标 | 说明 |
|---|---|---|
| **服务可用性** | `identity_up`, `cinder_up`, `glance_up` | 各服务在线状态 |
| **Nova 计算** | `nova_vcpus_max`, `nova_vcpus_used` | vCPU 总量/已用 |
| | `nova_memory_max`, `nova_memory_used` | 内存总量/已用 |
| | `nova_instances_max`, `nova_instances_used` | 实例配额/已用 |
| | `nova_local_storage_avail`, `nova_local_storage_used` | 本地存储 |
| **Neutron 网络** | `neutron_networks` | 网络总数 |
| | `neutron_agents_up` | 在线 agent 数 |
| | `neutron_floating_ips` | 浮动 IP 总数 |
| | `neutron_floating_ips_unassoc` | 未关联的浮动 IP |
| **Cinder 存储** | `cinder_volumes` | 卷总数 |
| | `cinder_volume_used_gb`, `cinder_volume_max_gb` | 存储用量/配额 |
| | `cinder_pool_free_gb` | 存储池剩余 |
| **Glance 镜像** | `glance_images`, `glance_image_bytes` | 镜像数量/大小 |
| **Identity** | `identity_users`, `identity_projects` | 用户数/项目数 |

#### 3.3.9 RabbitMQ 指标（新增）

| 类别 | 指标 | 说明 |
|---|---|---|
| **告警（1=紧急）** | `alarm_memory` | 内存 watermark 告警 |
| | `alarm_disk` | 磁盘空间不足告警 |
| | `alarm_fd` | 文件描述符告警 |
| **连接/通道** | `connections`, `channels` | 当前连接数/通道数 |
| | `conn_opened_rate`, `conn_closed_rate` | 连接开/关速率 |
| **队列深度** | `queues`, `messages_ready` | 队列总数/就绪消息 |
| | `messages_unacked`, `messages_total` | 未确认消息/总消息 |
| | `messages_paged_out` | 已换出到磁盘的消息 |
| **资源使用** | `memory_used_bytes`, `memory_limit_bytes` | 内存使用/上限 |
| | `disk_free_bytes`, `disk_limit_bytes` | 磁盘可用/watermark |
| | `erlang_procs`, `erlang_procs_limit` | Erlang 进程数/上限 |
| | `fd_used` | 文件描述符使用 |
| **Mnesia 事务** | `mnesia_failed_tx`, `mnesia_restarted_tx` | 失败/重启事务数 |

#### 3.3.10 自动探测 `detect_categories()`

```python
CATEGORY_PREFIXES = {
    "node":       "node_",
    "keepalived": "keepalived_",
    "ceph":       "ceph_",
    "openstack":  "openstack_",
    "rabbitmq":   "rabbitmq_",
}

def detect_categories(metric_names: list) -> list:
    detected = []
    name_set = set(metric_names)
    for cat, prefix in CATEGORY_PREFIXES.items():
        if any(n.startswith(prefix) for n in name_set):
            detected.append(cat)
    return detected
```

**原理**: 通过查询所有指标名，检查是否有某类前缀的指标存在。

### 3.4 文本报告生成 `build_text_report()`

**输出格式**:

```
=== VictoriaMetrics Metrics Report ===
Address     : http://192.168.88.128:8428
Report time : 2024-01-15 16:00:00 UTC
Query range : 2024-01-15 15:00:00 UTC  ~  2024-01-15 16:00:00 UTC  (last 1.0h)
Categories  : node, keepalived, ceph, rabbitmq
All metrics : 850 unique metric names

[NODE METRICS]  instances=3
  Instance: 192.168.1.10:9100
    Uptime        : 45d 3h 22m
    CPU cores     : 8
    CPU usage now : 65.3%
    CPU usage avg : 45.2%  max=92.1%
    Mem total     : 32.0 GB
    Mem used      : 78.5%  avail=6.9 GB
    Load 1/5/15   : 4.52 / 3.81 / 3.25
    ...

[KEEPALIVED METRICS]
  keepalived_up : 1
  VRRP kolla_internal_vip_51  intf=bond0.30  vrid=51: MASTER
  [WARN] auth_failure=4113532  (kolla_internal_vip_51)
  ...

[RABBITMQ METRICS]
  Alarm memory watermark alarm        : OK (0)
  Alarm disk free alarm               : [ALARM] (1)
  Connections                         : 150
  ...
```

### 3.5 main() 执行流程

```
1. 解析命令行参数
2. 计算时间范围
3. 健康检查（GET /health）
4. 获取所有指标名
5. 自动探测类别（或按用户指定）
6. 按类别采集：
   - node: Instant + Range 查询
   - keepalived/ceph/openstack/rabbitmq: Instant 查询
7. 根据 --output 输出 JSON 或文本
```

### 3.6 命令行参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `--url` | string | `http://192.168.88.128:8428` | VictoriaMetrics 地址 |
| `--range-hours` | float | `1.0` | 趋势查询时间窗口 |
| `--step` | int | `60` | 趋势查询步长（秒） |
| `--output` | choice | `text` | `text` / `json` |
| `--category` | string | `auto` | 指定类别或 `auto` |

---

## 四、通用设计模式

### 4.1 双输出模式

两个脚本均支持 `--output text` 和 `--output json`：

| 模式 | 用途 | 消费者 |
|---|---|---|
| **text** | 格式化的可读报告 | 大模型 LLM（直接分析） |
| **json** | 结构化数据 | 其他程序/自动化流程 |

### 4.2 stderr 日志 + stdout 数据分离

```python
print(f"[INFO] Fetching...", file=sys.stderr)  # 进度信息输出到 stderr
print(json.dumps(result))                       # 实际数据输出到 stdout
```

**好处**:
- 重定向 stdout 不影响进度日志
- 脚本可以直接 `python script.py > output.json` 得到干净数据
- LLM 读取 stdout 不受 `[INFO]` 干扰

### 4.3 异常容忍设计

所有 HTTP 请求失败时：
- 不抛出异常
- 打印 `[ERROR]` 到 stderr
- 返回空数据（`{}` 或 `[]`）
- 上层函数安全处理空数据

### 4.4 模式归一化 vs 原始样例

脚本同时保留两种数据：
- **归一化模式**：用于去重计数（去掉时间、IP、数字等变量）
- **原始样例**：保留一条完整原始日志（含真实 IP、时间等）供分析

### 4.5 时间戳统一处理

| 来源 | 格式 | 脚本处理 |
|---|---|---|
| Loki API | 纳秒级 Unix 时间戳字符串 | `int(ts) / 1e9` → UTC 格式化 |
| VictoriaMetrics API | 秒级 Unix 时间戳 | 直接 `datetime.fromtimestamp()` |
| Python 内部 | `time.time()` | 纳秒 `*1e9` 给 Loki，秒给 VM |

---

## 五、错误处理策略

### 5.1 Loki 脚本

| 异常场景 | 处理方式 |
|---|---|
| Loki 不可达 | HTTP 异常打印到 stderr，返回空数据 |
| 无 filename 标签 | 自动降级为 job 标签或通用选择器 |
| 查询结果过多 | Loki 侧 limit 截断，Python 侧去重压缩 |
| 正则匹配失败 | `classify_level()` 默认返回 `OTHER` |

### 5.2 VictoriaMetrics 脚本

| 异常场景 | 处理方式 |
|---|---|
| VM 不可达 | 健康检查失败打印 `[WARN]`，继续尝试查询 |
| 某指标不存在 | PromQL 返回空数组，`scalar()` 返回 `None` |
| 无对应类别指标 | `detect_categories()` 不返回该类，跳过采集 |
| PromQL 语法错误 | HTTP 400 错误打印到 stderr，该指标返回空 |

---

## 六、命令行速查

### 6.1 Loki 日志

```bash
# 查最近 6 小时 ERROR/WARN（默认）
python3 scripts/fetch_loki_logs.py --url http://192.168.88.128:3100

# 查最近 2 小时，指定 Loki 地址
python3 scripts/fetch_loki_logs.py --url http://10.0.0.5:3100 --hours 2

# 查全量日志（不推荐，context 很大）
python3 scripts/fetch_loki_logs.py --all-logs --hours 1

# 输出 JSON（给程序处理）
python3 scripts/fetch_loki_logs.py --output json --hours 12

# 自定义每文件展示的独特模式数
python3 scripts/fetch_loki_logs.py --max-patterns 20 --hours 24
```

### 6.2 VictoriaMetrics 指标

```bash
# 自动探测所有类别
python3 scripts/fetch_vm_metrics.py --url http://192.168.88.128:8428

# 只看 node 指标
python3 scripts/fetch_vm_metrics.py --url http://192.168.88.128:8428 --category node

# 查看 6 小时趋势
python3 scripts/fetch_vm_metrics.py --url http://192.168.88.128:8428 --range-hours 6

# 只看 RabbitMQ
python3 scripts/fetch_vm_metrics.py --url http://192.168.88.128:8428 --category rabbitmq

# 输出 JSON
python3 scripts/fetch_vm_metrics.py --url http://192.168.88.128:8428 --output json

# 同时看 node + ceph + rabbitmq
python3 scripts/fetch_vm_metrics.py --url http://192.168.88.128:8428 --category node,ceph,rabbitmq
```

---

## 附录：参考文件说明

`vm-metrics-analysis/references/` 目录下存放的是各 Exporter 的**原始指标样本**，用途：

| 文件 | 用途 |
|---|---|
| `keepalived.txt` | 新增 Keepalived 指标时确认 metric 名和标签 |
| `rabbitmq.txt` | 确认 RabbitMQ 指标名、HELP 注释、单位 |
| `ceph.txt` | 确认 Ceph 各组件 metric 名和 HELP 注释 |
| `openstack.txt` | 确认 OpenStack 各组件 metric 名 |
| `mysql.txt` | 备用，未来扩展 MySQL 监控时使用 |

> 这些文件较大（最大 2328 行），**不建议全量加载到 LLM context**。
> 建议用 `grep` 搜索指定指标名或标签。
