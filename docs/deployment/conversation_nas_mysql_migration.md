# NAS 会话元数据迁移操作手册

适用存储模式 `mysql-metadata`。将旧 `chats.json` 中的 Chat 目录和逻辑 Session 关联迁入 MySQL。会话正文、Turn 和归档由应用继续在原 NAS workspace 读写。

## 1. 准备数据库、目录和停写窗口

1. 为当前服务单元确认 NAS 工作目录根。普通租户使用 `<NAS 根>/<scope-id>/workspaces/default/chats.json`，其中 `scope-id` 使用线上采用的无前缀 scope 编码。source 系统级 `default` 用户使用 `<NAS 根>/default_<source-id>/workspaces/default/chats.json`。扫描会跳过该用户的编码目录，即使对应的未编码目录缺失。迁移进程和线上实例应看到同一个 workspace 绝对路径。
2. 确认迁移镜像中 `swe conversation-storage prepare --help`、`migrate --help` 和 `export-metadata --help` 均能执行。将 `CMB_CAAS_SERVICEUNITID` 设置为当前服务单元真实标识，并按应用现有配置注入目标 MySQL 连接参数和凭据。核对 `SWE_DB_NAME`。
3. 由 DBA 按下文检查目标应用库并选择建表或升级脚本。
4. 在正式迁移前停止会修改这些目录及会话的旧服务、任务和其他写入口，等在途写入完成。为数据库和 NAS 保留同一恢复点的备份，并记录旧镜像版本。可以提前运行扫描来检查范围，正式执行时必须保持扫描覆盖的 workspace 集合正确，并维持停写直至完成切换验收。

完成后，目标库具备 Chat、Session 和迁移状态表，NAS 和数据库均有可用的恢复点。

以下命令在能访问 NAS 和目标 MySQL 的环境中执行。将示例值替换为现场配置。审计目录应提前创建并可写，且位于业务 workspace 之外。

```bash
export CMB_CAAS_SERVICEUNITID='your-service-unit'
export SWE_DB_NAME='your-app-schema'
NAS_ROOT='/nas/swe'
AUDIT_DIR='/audit/conversation-migration'
MANIFEST="$AUDIT_DIR/workspace-scan.json"
```

### 数据库建表与升级

以下命令在仓库根目录执行。将 `DBA_CNF` 指向供 MySQL 客户端使用的连接配置，确保它连接到迁移进程使用的同一 MySQL 实例。

```bash
DBA_CNF='/path/to/dba-client.cnf'
mysql --defaults-extra-file="$DBA_CNF" --database="$SWE_DB_NAME" \
  < scripts/sql/conversation_mysql_metadata_inspect.sql
```

| 现有表结构 | 执行的 SQL |
| --- | --- |
| 四张目标表均不存在，即使应用库里已有其他业务表 | 一次执行完整安装脚本，创建 Chat、Session、来源和 workspace 四张表，并建立查询索引。 [完整建表](../../scripts/sql/conversation_mysql_metadata_install.sql) |
| 只有当前版本的基础 Session 和 Chat 两张表，尚未加元数据扩展 | 先执行元数据模式扩展，给已有表加列和 NAS 文件名唯一索引，并创建两张迁移状态表。再执行性能索引脚本。 [已有表升级](../../scripts/sql/conversation_mysql_metadata.sql) · [性能索引](../../scripts/sql/conversation_metadata_performance.sql) |
| 元数据扩展已完成，两个性能索引均缺失 | 只执行性能索引脚本。 [性能索引](../../scripts/sql/conversation_metadata_performance.sql) |
| 四张表及索引均已齐全 | 直接进入扫描。 |
| 存在部分升级、仅缺一个索引，或已有表与当前基础定义不同 | 根据检查结果与当前定义比对，由 DBA 仅应用缺失且兼容的 DDL。已有 Chat 行需要先核对归属及迁移状态，因为导入器会拒绝覆盖同一 scope 下已存在的 Chat。 [结构检查](../../scripts/sql/conversation_mysql_metadata_inspect.sql) · [当前基础定义](../../scripts/sql/conversation_metadata_tables.sql) |

全新安装和基础表升级二选一。四张目标表均不存在时，执行完整建表脚本。

```bash
mysql --defaults-extra-file="$DBA_CNF" --database="$SWE_DB_NAME" \
  < scripts/sql/conversation_mysql_metadata_install.sql
```

已有两张基础表且尚未安装扩展和性能索引时，按顺序执行下面两份脚本。

```bash
mysql --defaults-extra-file="$DBA_CNF" --database="$SWE_DB_NAME" \
  < scripts/sql/conversation_mysql_metadata.sql &&
mysql --defaults-extra-file="$DBA_CNF" --database="$SWE_DB_NAME" \
  < scripts/sql/conversation_metadata_performance.sql
```

执行完毕后再运行一次结构检查。DDL 逐条提交，失败后按实际结构补齐。分享索引 `swe_chat_shares` 由分享模块管理，独立于这四张会话迁移表。

## 2. 扫描 workspace

```bash
swe conversation-storage prepare \
  --workspace-root "$NAS_ROOT" \
  --manifest "$MANIFEST" \
  --workers 4
```

扫描按固定路径读取每个 scope 的 `workspaces/default`，检查 `chats.json` 的基本格式。清单记录 `migration_id`、目标库、NAS 根、每个 workspace 的路径和作用域，以及扫描异常。文件内容摘要由后续导入阶段处理。

根目录扫描默认最多使用 4 个线程，可用 `--workers` 调整。`--workers 1` 按顺序扫描。这个参数只影响 NAS 扫描，与数据库连接池独立。

| 输出 | 操作 |
| --- | --- |
| `prepared`，退出 0 | 核对 workspace 数量、路径和 scope。核对 NAS 上预期参与的租户和来源都在清单中。 |
| `partial_success`，退出 2，顶层 `scan_errors` 有记录 | 同一份清单可用于预览和导入。`migrate` 跳过这些目录，在 `failed` 中报告扫描错误，并继续处理已识别的 workspace。需要补迁时，先确认目录归属及线上路径，再为待补迁来源准备清单。 |
| `partial_success`，退出 2，某个 workspace 带 `scan_error` | 检查对应路径和文件。清单仍包含该 workspace，若仅修复其读取权限或 `chats.json`，可用原清单重新预览。 |

扫描结果文件必须是新文件。扫描范围只覆盖各 scope 的默认 Agent。扫描后如果新增了 workspace，在首次导入前重新扫描。默认 workspace 已存在、`chats.json` 缺失且 `sessions/` 和 `dialog/` 均为空时，工具按空工作区处理。

## 3. 只读预览（可选）

完成数据库准备、停写备份和 `prepare` 后，可以直接执行第 4 步。正式导入会在写入前检查待导入的 workspace。

```bash
swe conversation-storage migrate \
  --manifest "$MANIFEST"
```

预览重新读取当前 `chats.json`，校验 Chat 身份、元数据和 Session 文件名映射。加上 `--full-audit` 后，还会只读解析存在的关联 Session 和归档。预览不连接目标库，数据库结构和连接仍需按第 1 步核对。

完成后，`status=preview` 的 `validated` 列出通过的来源。若返回 `partial_success`，检查 `failed` 中的 `phase`、`scope`、`root` 和 `detail`。预览的文件版本只对应当次检查，正式执行时会重新读取。

## 4. 导入、校验并发布

```bash
swe conversation-storage migrate \
  --manifest "$MANIFEST" \
  --apply
```

命令连接目标库并获取该库的迁移命名锁。每个 workspace 以执行时读取的 `chats.json` 为准，每批最多写入 100 条 Chat。SQL 业务数据和批次进度同事务提交。工作区首次导入时，摘要与首批写入同事务提交。全部批次完成后，程序读回 SQL 目录并核对源文件，再按 `(service_unit_id, tenant_id, source_id)` 发布。

迁移只处理 `chat.user_id` 与清单租户 ID 相同的记录。若预检发现 Session 文件名冲突、会话记录格式有误或关联文件校验失败，报告会在 `failed` 中记录对应的 Chat 和 Session 信息。同一 workspace 中通过校验的 Session 继续入库，来源保持未发布。若整个 `chats.json` 无法解析，预检仍按 workspace 报错。

`--full-audit` 为可选项，预览和正式导入都可以启用。默认导入仍会校验 `chats.json`、NAS 路径和文件类型，并读回 SQL 数据核对。额外审计会解析存在的 Session 和归档，增加 NAS 读取和解析时间。

预览和导入默认最多并行处理 4 个独立来源，可用 `--workers` 调整。同一来源内的 workspace 及各自批次按顺序处理。正式导入并发还受进程环境变量 `SWE_DB_MAX_CONN` 限制，默认值为 10。在执行迁移的终端运行 `export SWE_DB_MAX_CONN=10`，或在运行迁移的容器中设置同名环境变量。`SWE_DB_MIN_CONN` 默认值为 2，须小于或等于连接池上限。为对比耗时，可在相同输入和数据库条件下分别使用 `--workers 1` 和 `--workers 4`。

| 结果字段 | 含义 |
| --- | --- |
| `published` | 本次导入、校验并发布的来源。 |
| `already_published` | 同一清单下此前已发布的来源，本次直接跳过。 |
| `skipped_workspaces` | 进度回执与 SQL 记录匹配，本次跳过的 workspace。 |
| `failed` | 失败来源、workspace 或 Session，可能已有部分批次提交。只要该来源存在失败，它就保持未发布。 |

`ready` 且退出 0 表示本次清单中的来源已全部处理。退出 2 时查看结构化结果，确认是否为 `partial_success`。部分成功时保持停写，修复剩余失败并继续迁移。保留原始清单、命令输出以及生成的 `<清单文件名>.progress.json`。

切流前，复制核验脚本到审计目录，将副本开头两个变量分别替换为 manifest 的 `migration_id` 和当次使用的 `CMB_CAAS_SERVICEUNITID`，再执行副本。核对 CLI 报告、预期迁移范围和实际 SQL 结果。 [迁移后核验 SQL](../../scripts/sql/conversation_mysql_metadata_verify.sql)

```bash
cp scripts/sql/conversation_mysql_metadata_verify.sql "$AUDIT_DIR/"
# 编辑审计目录中的副本，再执行下方命令
mysql --defaults-extra-file="$DBA_CNF" --database="$SWE_DB_NAME" \
  < "$AUDIT_DIR/conversation_mysql_metadata_verify.sql"
```

`swe_conversation_metadata_sources.published=1` 表示来源已发布，`0` 表示已登记但尚未发布。扫描失败或在登记前失败的来源可能没有 SQL 记录，因此还要看 CLI 的 `failed`。在恢复写入前，对已发布的 workspace 核对 `imported_count` 与 `live_chat_count` 是否一致。多个 Chat 可以关联同一个 Session。

## 5. 续跑和失败处理

| 场景 | 操作及效果 |
| --- | --- |
| 命令中断、连接故障或命名锁被占用 | 确认旧进程退出或连接恢复后，用原清单重跑第 4 步。相同输入可依据数据库进度继续。 |
| workspace 的预检失败，或首批写入失败并已回滚 | 修复原路径下的文件后，用原清单重跑。导入会重新读取修好的版本。 |
| workspace 已有已提交的导入批次，随后 `chats.json` 改变 | 恢复与数据库绑定摘要一致的文件后，使用原清单续跑。无法恢复时保留现场并核对已提交数据，避免在清单中替换摘要或手工调整进度。 |
| 来源需要绑定修复后的新清单 | 仅用于未发布来源。新旧清单中该来源的 workspace 成员必须一致，已绑定 workspace 的路径和摘要必须匹配。使用 `migrate --manifest <新清单> --resume-from <原清单> --apply --full-audit`。新清单应只包含拟修复来源，已发布来源保留各自原清单。 |

## 6. 切换并验收

保持旧写入全部停止，核对所有计划迁移的历史 workspace 都已覆盖，且对应来源全部通过迁移和 SQL 核验。处理扫描和导入报告中的所有迁移范围内的失败项后，再统一配置 `SWE_CONVERSATION_STORAGE_AUTHORITY=mysql-metadata`，沿用匹配的 `CMB_CAAS_SERVICEUNITID`、目标 MySQL 和原 NAS 挂载路径。

先启动受控实例，检查 Chat 列表、旧历史和续聊，确认新消息写到预期的原 NAS 路径，重启后能读回。再验证全新租户可以初始化、打开空列表并开始首轮会话。全部实例完成切换后恢复写入和流量。

完成后，Chat 目录以 MySQL 为准，应用继续用原 NAS 文件读写会话内容。`published` 供离线导入、续跑和核验使用。应用会为全新租户的空 workspace 自动登记路径，查询不到 Chat 时返回空列表。

## 7. 回退

若新模式尚未承接业务写入，可使用停写时保留的原目录和 NAS 恢复点启动旧版。若新模式已接收写入，先停止新旧写入入口，记录当前 NAS 恢复点，再从当前 SQL 导出旧版目录。

```bash
swe conversation-storage export-metadata \
  --destination "$AUDIT_DIR/rollback-export"
```

导出目录必须尚不存在。命令按 `CMB_CAAS_SERVICEUNITID` 导出该服务单元已登记的 workspace，包括切换后新增的租户。如果该服务单元还有 `published=0` 的迁移来源，全量导出会停止，以免将部分导入的目录用于回退。导出成功后生成 `report.json` 和每个 workspace 的旧格式 `chats.json`。有多个服务单元时，分别设置环境变量并导出至独立目录。`--manifest` 仍可用于只导出指定迁移记录。

根据报告，将导出目录与同一恢复点的 NAS 配套。先验证旧版能读取历史并继续写入，再切换流量。
