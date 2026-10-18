# 记忆工具

Python 3.11+ 标准库 CLI，用于条目校验、词法检索、上下文生成、并发写入、事务恢复和派生索引。无需安装依赖，无后台服务，不访问网络，不执行 Git。本目录包含 `memory.py` 和 `memlib/`，应保持整体相对布局。

这是工具说明，不是记忆保存授权。是否启用、允许保存什么、由谁协调写入，遵守工作区的[记忆管理规则](../../记忆/管理规则.md)。数据合同见[格式规范](../../记忆/格式规范.md)。模板不预装用户偏好或真实记忆。

## 根目录与初始化边界

工具按 `Path(__file__).resolve().parents[3]` 求工作区根，即 `工作区/中枢/工具/记忆工具/memory.py` 对应的 `工作区`。`--root` 可显式指定隔离或迁移根；相对值按当前进程目录解释。把工具随意移到其他层级会改变默认根，不应靠修改源码补路径。

CLI **没有 `init` 命令**，不会给错误根创建配置。初始化由[工作区管理程序](../工作区管理/工作区管理.py)负责；在工作区根运行 `py -3 -X utf8 中枢/工具/工作区管理/工作区管理.py init`，为新工作区生成独立配置与启用状态。管理程序从自身未解析链接的绝对路径 `parents[3]` 定位根，并检查祖先和管理目录链接；模板信息位于 `中枢/模板/模板信息.json`，包内不可重写的出厂来源基线位于 `中枢/模板/出厂文件清单.json`，不是实时状态。未经初始化的模板源码目录不是运行库，不能在其中运行 `doctor`、`context`、`reindex` 或写入命令来冒充验收。

根必须已存在，且有合法的 `中枢/记忆/配置.json`。schema v1 **恰好六个字段**，缺失字段、未知字段、布尔值冒充数字等会拒绝：

| 字段 | 合同 |
| --- | --- |
| `schema_version` | 整数 `1` |
| `workspace_id` | 独立稳定标识，1–100 位，首字符为 ASCII 字母或数字，其余为字母、数字、`.`、`_`、`-`；不使用示例值作为所有工作区的共同身份 |
| `lock_timeout_seconds` | 数字，`0 < x <= 60`；建议初值 `5` |
| `query_limit` | 整数 `1..1000`；建议初值 `8` |
| `context_max_chars` | 整数 `100..1000000`；建议初值 `6000` |
| `candidate_review_days` | 整数 `1..3650`；建议初值 `30` |

供工作区管理程序复用的接口：将本工具目录加入模块搜索路径后，`memlib.config.validate_config(data, origin)` 返回不可变的 `MemoryConfig`，`load_config(root: Path)` 从指定根读取同一配置；`MemoryConfig.to_dict()` 导出六字段。配置错误抛出 `memlib.errors.SchemaError`，根或配置不存在可抛出 `UsageError`，二者均继承 `MemoryError`。这些接口不负责启用授权或初始化。

## 常用命令

以下命令以**已经初始化、已确认启用的工作区根**为当前目录。Windows 可使用 `py -3 -B -X utf8`；其他系统可替换为 Python 3.11+ 的 `python3 -B -X utf8`。`-B` 禁止在工具旁生成字节缓存，不影响记忆库自己的锁、事务与索引。

```text
py -3 -B -X utf8 ./中枢/工具/记忆工具/memory.py --help
py -3 -B -X utf8 ./中枢/工具/记忆工具/memory.py doctor --json
py -3 -B -X utf8 ./中枢/工具/记忆工具/memory.py validate --json
py -3 -B -X utf8 ./中枢/工具/记忆工具/memory.py context --cwd . --query "相关主题" --json
py -3 -B -X utf8 ./中枢/工具/记忆工具/memory.py search "相关主题" --cwd . --scene engineering --json
py -3 -B -X utf8 ./中枢/工具/记忆工具/memory.py show mem-example --json
py -3 -B -X utf8 ./中枢/工具/记忆工具/memory.py review --json
py -3 -B -X utf8 ./中枢/工具/记忆工具/memory.py reindex --check --json
```

`--root`、`--json` 可放在子命令前或后。从工作区以外运行，或指定隔离根时，`search`/`context` 还要用 `--cwd` 指定该根内部的实际目录；仅设置 `--root` 不会把进程当前目录自动改到那里。`--scene` 可重复，task 范围额外需要匹配的 `--task-id`。

### 空库与可写锁

合法配置下的空条目库是正常起点，不需要种子偏好。`validate` 可返回条目数 `0`，`search` 返回 `items=[]`、`total_matched=0`，`context` 返回 `items=[]`、`total_applicable=0`。未建索引时 `index_status="missing"` 并不等于已有事实损坏；`doctor` 可能报告布局或派生状态警告。不能把空结果改写成已经学到用户偏好。

**只读业务命令也取独占协调锁**：`中枢/记忆/运行/memory.lock`。它们不改条目事实或凭据，但可能创建运行目录与锁文件，因此 root 必须允许该位置写入。只读介质或不可写锁路径会失败；`--dry-run` 也不是零文件系统接触。锁文件释放后保留，不得靠删除锁文件解锁。

Windows 使用 `msvcrt` 字节锁，POSIX 使用 `fcntl.flock`。锁只协调遵守协议的进程，不阻止普通编辑器绕过工具，不承诺跨设备或网络盘同步。

## 新增和更新请求

以下 JSON **只是纯虚构的隔离演示数据**，不是作者或使用者的事实，也不随模板写入条目库。只在临时隔离工作区演练，或替换为有依据且已获授权的真实请求后使用。

`add-request.json`：

```json
{
  "metadata": {
    "id": "mem-example",
    "kind": "fact",
    "subject_key": "example.fixture",
    "status": "active",
    "scope": {"level": "workspace", "target": ".", "scenes": [], "task_id": null},
    "pinned": false,
    "tags": ["演示"],
    "sources": [{"kind": "artifact_verified", "locator": "fixture://example/add", "source_date": null, "observed_at": null}],
    "valid_from": null,
    "valid_until": null,
    "review_after": null,
    "end_condition": null,
    "superseded_by": null
  },
  "body": "# 虚构演示条目\n\n## 结论\n\n这是用于接口演练的测试文本。\n\n## 使用边界\n\n仅限隔离演示，不表示任何人的真实事实。\n",
  "reason": "隔离演示新增",
  "actor": "example-tester"
}
```

```text
py -3 -B -X utf8 ./中枢/工具/记忆工具/memory.py add --input ./add-request.json --dry-run --json
py -3 -B -X utf8 ./中枢/工具/记忆工具/memory.py add --input ./add-request.json --json
py -3 -B -X utf8 ./中枢/工具/记忆工具/memory.py show mem-example --json
```

新增可省略 `metadata.id`，工具生成 `mem-` 加 UUID hex。成功返回 `result="created"`，ID 在 `operations[0].id`，新版本在 `operations[0].revision_after`，新原始字节 SHA-256 在 `operations[0].sha256_after`。

更新请求不是仅传变化字段的 JSON Merge Patch。它需提供 `metadata` 与完整 `body`，保留所有仍需保留的可写字段；缺必填字段会拒绝，省略可选字段可能归为默认值而清空旧值。可靠做法是读取 `show` 的 `entry.metadata`，仅选取 `memlib.schema.WRITABLE_FIELDS`，再修改目标字段。不得回传 `schema_version`、`revision`、`created_at`、`updated_at`、`last_verified_at` 这些托管字段。

`update-request.json` 可保留上例的 metadata，只把 `body` 改为 `"# 虚构演示条目\n\n## 结论\n\n这是修正后的隔离测试文本。\n"`，把 `reason` 改为 `"隔离演示修正"`。从最新 `show` 响应取 `entry.revision` 和 `entry.sha256`，替换以下占位值：

```text
py -3 -B -X utf8 ./中枢/工具/记忆工具/memory.py update mem-example --input ./update-request.json --expected-revision <最新revision> --expected-sha256 <最新sha256> --json
```

CAS 版本/SHA 不符返回 `3`，不会强行覆盖。重新 `show`、理解并发差异后再构造新请求，不能只把旧请求换上新版本号。实质更新返回 `result="updated"`，无变化返回 `"no_change"` 且不制造新 revision、事务或凭据；仅核实可返回 `"verified"` 而不增加 revision。

`last_verified_at` 只能通过顶层 `verification={"verified_at":"带时区时间","source_locator":"核实依据","reason":"核实理由"}` 修改。读取、导入或重建索引不等于复核。`candidate` 需要正文中的 `## 待澄清问题`；没有复核时间时按创建时间加配置天数设置提示，不表示到期删除。

## JSON 与退出码合同

`--json` 时 stdout 是一个合法 JSON 对象，诊断在 stderr。基础字段为：

- `schema_version`、`tool_version`、`command`、`ok`。
- `data_status`、`audit_status`、`index_status`、`git_status` 分别报告数据、审计、派生索引、Git 状态。CLI 不执行 Git，`git_status` 恒为 `"not_attempted"`；事务凭据落盘不等于已提交。
- 请求级失败通常 `ok=false`，并提供 `error.code/kind/message/details`。完整检查结果可能 `ok=true` 但退出非零；不要只看 `ok`。

| 命令 | 主要字段 |
| --- | --- |
| `doctor` | `findings[]`、`counts.entries/parse_errors/pending_transactions/errors/warnings`、`paths`、`generated_links`、`derived`；检查不自动修复 |
| `validate` | `counts.entries/parse_errors/invariant_errors/conflict_groups`、`errors[]`、`findings[]`、`conflicts[]`、`entries[]` |
| `show` | `entry.id/title/path/revision/sha256/metadata/body/raw/conclusion` |
| `search` | `items[]`、`total_matched`、`returned`、`tokens`、`conflicts[]`、`cwd/scenes/task_id/at` |
| `context` | `items[]`、`total_applicable`、`markdown`、`char_count`、`omitted_count`、`omitted_ids[]`、`more_commands[]`、`conflicts[]`；条目可有 `body_omitted=true` |
| `add/update/apply` | `result`、`dry_run/no_change/would_change`、`operations[]`、`transaction_id`、`credential`、`derived`、`findings[]` |
| `reindex --check` | `fresh`、`derived.index/links`；陈旧用 `fresh=false` 表达，仍可退出 `0` |
| `recover` | `result`、`mode`、`transactions[]`、`blocked`、`changed`、`derived`、`quarantined[]` |

退出码：`0` 成功；`2` 参数或 schema 错误；`3` 版本、SHA 或结构冲突；`4` 锁超时；`5` 有待恢复事务；`6` I/O；`7` 派生刷新失败。自动检查应同时看退出码、`data_status` 和相应计数或新鲜度。

`doctor` 即使发现某些严重 finding 也可能返回 `0`，有待恢复事务返回 `5`。`reindex` 无成对生成标记时不猜测或覆盖人工区，可能已写索引但返回 `7`；应由主会话先核实标记。链接区要求唯一一对：

```text
<!-- BEGIN GENERATED MEMORY LINKS -->
<!-- END GENERATED MEMORY LINKS -->
```

## 事务、恢复与索引

`apply --patch-file` 将明确列出的 `add/update/delete` 组成一个事务，最多 200 项，不支持同一事务重复目标 ID。更新和删除仍需预期 revision/SHA；多条替代关系应在同一事务完成。删除不是日常停用的替代，优先按政策修改状态。

```text
py -3 -B -X utf8 ./中枢/工具/记忆工具/memory.py recover --dry-run --json
py -3 -B -X utf8 ./中枢/工具/记忆工具/memory.py recover --apply --json
py -3 -B -X utf8 ./中枢/工具/记忆工具/memory.py reindex --json
```

未完成事务会阻断正常上下文，先检查再恢复。遇到第三种内容哈希时停止覆盖并保留双方材料；数据或凭据已提交但派生失败时，不应回滚真实条目来伪装完整成功。`中枢/记录/记忆变更/` 是工具生成的变更审计凭据，不是认证秘密，但仍不应随意公开本地来源线索。

查询直接扫描权威条目，不信任旧索引替代事实；索引缺失、损坏、陈旧会报告状态，仍可查询原文。检索是词法匹配，不是向量语义召回；没有命中可换同义关键词，不凭空补充事实。`review_after` 到期只提示，`valid_until` 到期才从当前查询排除；`--history` 不把历史数据升级为当前指令。

## 开发基线测试

项目测试位于项目根的 `测试/记忆工具/`，不随工具目录混入运行数据。以项目根为当前目录执行：

```text
py -3 -B -X utf8 ./测试/记忆工具/run_baseline.py
```

测试帮助文件只引用模板内 CLI，功能测试自建临时 root，不在模板或生产库运行命令。运行器禁止源码字节缓存，把本轮日志和结构化结果写入项目 `本地/工具提取验证/`；失败和跳过如实记录。Windows junction 夹具仅在 Windows 执行，创建能力不足时明确跳过。测试通过不等于已在所有 Python 3.11+ 版本、平台、网络盘或实际用户客户端验收。
