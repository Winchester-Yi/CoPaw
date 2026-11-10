# 已有 NAS 会话迁移操作单

> 已由 [v3 迁移及上线全流程方案](2026-11-02-conversation-migration-and-launch-runbook.md) 替代。
> 下文保留 `881ab1764` 的历史流程；当前版本已禁用 v2 import/install 写入口，不要按下文执行。

日期：2026-11-02，按提交 881ab1764 的命令和格式核对。本文仅交付操作说明，未执行迁移。
采用上线后修复新版、不退回旧版的路线；数据库和 NAS 的一致性备份与恢复仍要保留。

## 1. 准备最终旧数据

先关闭会话入口、分享访问、定时任务和重试，等活动任务结束，再停止全部旧实例和后台写入。
在整个导入和核对期间保持停止，防止自动拉起。

运维将全部旧 workspace 完整复制/快照到独立位置，包含 chats.json、sessions、dialog、
隐藏 ledger/journal 和恢复所需内容。冻结副本的目录与文件均不得带写权限；不要为通过
检查而修改原业务 NAS 权限。当前导入器拒绝符号链接。

三个位置不要混淆（以下均为要替换的示例）：

| 用途 | 示例 |
| --- | --- |
| 只读旧副本，导入从这里读 | /mnt/migration-source/service-a/workspace-default |
| 原 NAS 根，新 bundle 写入此处的 bundles/ | /mnt/service-a/nas |
| 原业务 workspace，新版继续使用 | /mnt/service-a/nas/workspaces/default |

冻结源与目标 NAS 根必须互不包含、不重叠；不同服务的目标 NAS 根也必须隔离。
MySQL 保存目录、状态、索引和引用，正文及完整恢复内容发布成 NAS bundle，旧文件不自动删除。
附件和其他业务文件继续保留。

## 2. 使用完整源码和原数据库配置

发布包必须带 scripts/sql/。从审核过的源码根目录执行：

~~~bash
export SWE_PYTHON=/path/to/project/venv/bin/python
export PYTHONPATH="$PWD/src"
export AUDIT_DIR=/audit/conversation-migration-20261102
mkdir -p "$AUDIT_DIR"
"$SWE_PYTHON" -m swe conversation-storage --help
~~~

数据库使用部署注入的 SWE_DB_HOST、SWE_DB_PORT、SWE_DB_USER、SWE_DB_ACCESS、SWE_DB_NAME。
不要在清单或日志中保存密码。SWE_DB_ACCESS 的当前读取逻辑会去掉前四个字符，沿用既有
部署前缀约定，不要直接塞裸密码；这只是字符串处理，不是迁移工具执行解密。
SWE_DB_NAME 始终填目标应用库，不要改成 staging；工具自动创建暂存库。

迁移账号须有相应建库、DDL、读写、跨 schema 切表和元数据/锁查询权限。原应用库须已有
八旧列的 swe_chat_shares，不能预装六张新会话表。结构不符先处理，不能临时造假表绕过。

## 3. 写好数据归属清单和分享清单

在 AUDIT_DIR 下准备 workspaces.json。每个冻结 workspace 一项，全部服务放在同一个
协调作业里，不能每服务各自安装共享七表。示例身份必须替换：

~~~json
[
  {
    "root": "/mnt/migration-source/service-a/workspace-default",
    "service_unit_id": "SERVICE_A",
    "tenant_id": "default",
    "source_id": "RMASSIST",
    "agent_id": "default",
    "session_paths": [
      {
        "user_id": "alice",
        "channel": "console",
        "session_id": "original-session-id",
        "path": "sessions/alice_original-session-id.json"
      }
    ]
  }
]
~~~

工具从 chats.json 取关联身份并按旧规则找文件。session_paths 用于实际路径不同或未在
chats.json 登记的独立 Session；不是要求手工重抄所有聊天。身份从可信记录取得，不从
散列/乱码文件名反推。默认 registry/sessions/dialog 路径不同，可指定 registry_path、
sessions_dir、dialog_dir。未映射 Session、未登记归档、身份冲突、缺文件都会阻断迁移。

shares.json 保存停写边界的原分享八字段，并补 service_unit_id、snapshot_workspace_index、
snapshot_path。snapshot_workspace_index 为 workspaces.json 从 0 开始的序号；将对应分享
JSON 正文复制到该冻结 workspace 内，用相对路径填 snapshot_path。原 snapshot_key 保留。
原 Chat 已删除的分享也不能漏。确认原表真的没有分享时才用 []。

~~~json
[
  {
    "service_unit_id": "SERVICE_A",
    "token": "original-token",
    "chat_id": "original-chat-id",
    "creator_id": "alice",
    "tenant_id": "default",
    "snapshot_key": "original/snapshot/key.json",
    "created_at": "2026-10-01T00:00:00Z",
    "access_count": 17,
    "last_access_at": null,
    "snapshot_workspace_index": 0,
    "snapshot_path": "migration-share-snapshots/original-token.json"
  }
]
~~~

字段取真实值，时间正确表示时区。清单含用户身份和分享 token，按敏感数据管理。

## 4. 创建迁移进度文件，只做一次

CLI 没有自动生成清单的子命令，使用现有 Python API。先替换所有占位值；程序不连接数据库。
不退旧版也仍需记录实际旧镜像 digest，这是当前 SourceBoundary 校验要求，不得伪造。

~~~bash
"$SWE_PYTHON" - <<'PY'
import json
import os
from pathlib import Path
from swe.app.conversation_storage.migration.manifest import MigrationManifest, SourceBoundary

audit = Path(os.environ["AUDIT_DIR"])
manifest = MigrationManifest.create(
    audit / "manifest.json",
    SourceBoundary(
        snapshot_id="REPLACE_WITH_REAL_SNAPSHOT_ID",
        captured_at="REPLACE_WITH_REAL_TIME_WITH_TIMEZONE",
        old_image_digest="REPLACE_WITH_REAL_IMAGE_AT_SHA256_DIGEST",
        evidence_refs=("REPLACE_WITH_STOP_RECORD", "REPLACE_WITH_READONLY_RECORD"),
    ),
    json.loads((audit / "workspaces.json").read_text()),
)
manifest.data["offline"] = {
    "application_schema": "REPLACE_WITH_REAL_APPLICATION_DATABASE",
    "nas_roots": {"SERVICE_A": "/mnt/service-a/nas"},
    "shares": json.loads((audit / "shares.json").read_text()),
}
manifest._persist()
print(manifest.path)
PY
~~~

captured_at 用实际时间，如 2026-11-02T12:00:00+08:00。镜像格式为
仓库/镜像@sha256:64位小写十六进制。nas_roots 必须精确覆盖全部服务及各自原 NAS 根。
application_schema 与 SWE_DB_NAME 相同。当前 API 通过 _persist 保存 offline 配置。

已经导入的作业继续用原 manifest；不要删进度、重建迁移 ID 或随意改源和目标。
只盘点还未导入时发现输入错误，可修正真实输入并重新准备清单；已导入则先保留现场分析。

## 5. 按顺序运行盘点、导入、核对和安装

逐条执行，每条检查退出码和报告，失败立即停止，不要整段不看结果直接跑完。

~~~bash
# 以下三项是预览，不连接数据库、不写业务数据
"$SWE_PYTHON" -m swe conversation-storage inventory --manifest "$AUDIT_DIR/manifest.json" > "$AUDIT_DIR/01-inventory.json"
"$SWE_PYTHON" -m swe conversation-storage plan --manifest "$AUDIT_DIR/manifest.json" > "$AUDIT_DIR/02-plan.json"
"$SWE_PYTHON" -m swe conversation-storage import --manifest "$AUDIT_DIR/manifest.json" > "$AUDIT_DIR/03-import-preview.json"

# 核对 blockers=[]、所有 source_scopes、应用库、暂存库和 NAS 根后，才正式导入
"$SWE_PYTHON" -m swe conversation-storage import --manifest "$AUDIT_DIR/manifest.json" --apply > "$AUDIT_DIR/04-import.json"

# 连接暂存库，完整比较旧/新内容、顺序、状态、归档、恢复信息与分享
"$SWE_PYTHON" -m swe conversation-storage verify --manifest "$AUDIT_DIR/manifest.json" > "$AUDIT_DIR/05-verify.json"

# 仅在 verify passed=true、blockers=[] 且退出 0 后继续
"$SWE_PYTHON" -m swe conversation-storage install --manifest "$AUDIT_DIR/manifest.json" > "$AUDIT_DIR/06-install-preview.json"
"$SWE_PYTHON" -m swe conversation-storage install --manifest "$AUDIT_DIR/manifest.json" --apply > "$AUDIT_DIR/07-install.json"
"$SWE_PYTHON" -m swe conversation-storage inspect --manifest "$AUDIT_DIR/manifest.json" > "$AUDIT_DIR/08-inspect.json"
~~~

inventory 的 CLI 输出目前是预览报告，不一定带 ready 字段，检查 blockers 和 source_scopes。
import --apply 自动创建作业暂存 schema 和七表，分批写元数据、发布新 NAS bundle。
应用库此时没切换，原文件保留。中断后，在源/目标不变前提下用同 manifest 重跑 import。

install --apply 再次完整核对，自动收紧分享服务字段并检查权限、结构、锁和原子切表能力，
最后整批安装七表。不要自行提前执行建表、finalize 或手工 INSERT 到应用库。
保存 manifest.json.install.json，成功 phase 为 installed_before_startup。
应用库应有六张 swe_conversation_* 加扩展后的 swe_chat_shares，暂存库剩生成旧分享表。
inspect 读取本地 receipt 并重新盘点源，不是数据库独立对账命令。
切表断线或 rename_pending 时，保持所有 writer 停止，让 DBA 查询实际表名；不要重跑安装。

## 6. 给新版配置原 workspace 与新权威

每服务单独生成运行时文件 /etc/swe/conversation-scopes.json，示例：

~~~json
{
  "version": 2,
  "service_unit_id": "SERVICE_A",
  "nas_root": "/mnt/service-a/nas",
  "workspaces": [
    {
      "root": "/mnt/service-a/nas/workspaces/default",
      "scope": ["SERVICE_A", "default", "RMASSIST", "default"]
    }
  ]
}
~~~

这里 root 是原业务 workspace，不是冻结副本。scope 顺序为服务、租户、来源系统、Agent。
全部已有 workspace 都列入。不同服务各自 NAS 和运行时文件，连接同一个共享应用库。

在实际部署中注入，密钥用 Secret，示例：

~~~text
CMB_CAAS_SERVICEUNITID=SERVICE_A
SWE_CONVERSATION_STORAGE_AUTHORITY=mysql
SWE_CONVERSATION_SCOPE_MANIFEST=/etc/swe/conversation-scopes.json
SWE_CONVERSATION_ARCHIVE_CURSOR_SECRET=<稳定随机密钥>
~~~

同一服务的副本服务 ID、NAS 和游标密钥保持一致，数据库仍指应用库。
authority 默认 legacy，不显式改成 mysql 不会切到新数据。
保持外部入口关闭，先启动一个新版实例，确认 schema readiness、旧会话读取、新问答、
停止/重连、分享和服务/用户隔离；再扩容验证多实例，最后开放入口与定时任务。

## 7. 不退旧版时故障处理

保持入口关闭、保存现场，在新版路径修复或恢复；结果未知的操作先对账，不重跑外部工具。
不能靠删除 bundles、反向切表或恢复停机前 NAS 丢掉新增问答和计数。原始备份只读保留，
暂不启用物理 GC；上线后仍需验证新版全范围一致性备份和恢复。

执行前需要：服务 ID、原 NAS 根、全部 workspace 目录与真实身份、独立冻结副本、目标应用库
及连接注入、全部旧分享记录和正文、实际旧镜像 digest、停写和快照时间。本文不猜生产值。
