#!/bin/bash
# 备份解压/恢复脚本 for CoPaw
# Usage: decompress.sh [OPTIONS]
# Options:
#   --zip-dir DIR         zip 文件所在目录
#   --working-dir DIR     目标工作目录（恢复位置）
#   --secret-dir DIR      目标密钥目录
#   --tenants LIST        指定租户（逗号分隔，空则恢复所有 zip）
#   --rollback-dir DIR    回滚备份目录
#   --task-id ID          任务 ID（用于回滚目录命名）

set -euo pipefail

# 默认路径（可通过环境变量或参数覆盖）
WORKING_DIR="${SWE_BACKUP_SCRIPT_WORKING_DIR:-/opt/deployments/app/working}"
SECRET_DIR="${SWE_BACKUP_SCRIPT_SECRET_DIR:-/opt/deployments/app/working.secret}"
ZIP_DIR="${ZIP_DIR:-}"
ROLLBACK_DIR="${ROLLBACK_DIR:-}"
TASK_ID="${TASK_ID:-}"
TENANTS="${TENANTS:-}"

# 解析命令行参数
while [[ $# -gt 0 ]]; do
    case "$1" in
        --zip-dir) ZIP_DIR="$2"; shift 2 ;;
        --working-dir) WORKING_DIR="$2"; shift 2 ;;
        --secret-dir) SECRET_DIR="$2"; shift 2 ;;
        --tenants) TENANTS="$2"; shift 2 ;;
        --rollback-dir) ROLLBACK_DIR="$2"; shift 2 ;;
        --task-id) TASK_ID="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# 检查必须参数
if [[ -z "$ZIP_DIR" ]]; then
    echo "Error: --zip-dir is required" >&2
    exit 1
fi

if [[ ! -d "$ZIP_DIR" ]]; then
    echo "Error: ZIP directory does not exist: $ZIP_DIR" >&2
    exit 1
fi

echo "========================================"
echo "CoPaw Backup Decompression Script"
echo "========================================"
echo "ZIP dir: $ZIP_DIR"
echo "Working dir: $WORKING_DIR"
echo "Secret dir: $SECRET_DIR"
echo "Rollback dir: $ROLLBACK_DIR"
echo "Task ID: $TASK_ID"
echo "========================================"

# 解析租户列表
TENANT_ARRAY=()
if [[ -n "$TENANTS" ]]; then
    TENANT_ARRAY=($(echo "$TENANTS" | tr ',' ' '))
fi

# Never merge immutable memory generations through the legacy copy-based path.
if [[ -d "$WORKING_DIR" ]] && \
   [[ -n "$(find "$WORKING_DIR" \( -name state-v2.json -o -name .turn-ledger \) -print -quit)" ]]; then
    echo "Error: Versioned memory storage requires stopped writers and Python restore into a fresh directory." >&2
    exit 1
fi
for memory_archive in "$ZIP_DIR"/*.zip; do
    [ -f "$memory_archive" ] || continue
    if unzip -Z1 "$memory_archive" | grep -Eq '(^|/)(state-v2\.json$|\.turn-ledger/)'; then
        echo "Error: Versioned memory storage requires stopped writers and Python restore into a fresh directory." >&2
        exit 1
    fi
done

# 创建回滚目录
ROLLBACK_PATH=""
if [[ -n "$ROLLBACK_DIR" && -n "$TASK_ID" ]]; then
    ROLLBACK_PATH="$ROLLBACK_DIR/$TASK_ID"
    mkdir -p "$ROLLBACK_PATH"
fi

# 遍历 zip 文件进行恢复
restored_count=0
rollback_count=0
for zip_file in "$ZIP_DIR"/*.zip; do
    # 检查文件是否存在
    [ -f "$zip_file" ] || continue

    # 获取租户 ID
    tenant=$(basename "$zip_file" .zip)

    # 如果指定了租户列表，只恢复指定的租户
    if [[ ${#TENANT_ARRAY[@]} -gt 0 ]]; then
        skip=true
        for t in "${TENANT_ARRAY[@]}"; do
            if [[ "$tenant" == "$t" ]]; then
                skip=false
                break
            fi
        done
        [[ "$skip" == true ]] && continue
    fi

    echo "Restoring tenant: $tenant"

    tenant_dir="$WORKING_DIR/$tenant"

    # 创建回滚备份（如果目标目录已存在）
    if [[ -n "$ROLLBACK_PATH" && -d "$tenant_dir" ]]; then
        rollback_zip="$ROLLBACK_PATH/${tenant}.zip"
        echo "Creating rollback backup: $rollback_zip"

        tmpdir=$(mktemp -d)

        # 复制当前工作目录内容
        cp -r "$tenant_dir"/. "$tmpdir/" 2>/dev/null || true

        # 复制 .secret 子目录
        if [ -d "$tenant_dir/.secret" ]; then
            mkdir -p "$tmpdir/.secret"
            cp -r "$tenant_dir/.secret"/. "$tmpdir/.secret/" 2>/dev/null || true
        fi

        # 复制 providers 配置
        if [ -d "$SECRET_DIR/$tenant/providers" ]; then
            mkdir -p "$tmpdir/.providers"
            cp -r "$SECRET_DIR/$tenant/providers"/. "$tmpdir/.providers/" 2>/dev/null || true
        fi

        # 压缩回滚备份
        (cd "$tmpdir" && zip -r "$rollback_zip" . >/dev/null 2>&1)
        rm -rf "$tmpdir"

        echo "ROLLBACK:$tenant:$rollback_zip"
        rollback_count=$((rollback_count + 1))
    fi

    # 创建目标目录
    mkdir -p "$tenant_dir"

    # 解压到临时目录
    tmpdir=$(mktemp -d)
    unzip -q "$zip_file" -d "$tmpdir"

    # 复制工作目录内容（排除 .secret 和 .providers）
    for item in "$tmpdir"/*; do
        name=$(basename "$item")
        [[ "$name" == ".secret" || "$name" == ".providers" ]] && continue
        cp -r "$item" "$tenant_dir/" 2>/dev/null || true
    done

    # 复制隐藏文件（.开头的文件）
    for item in "$tmpdir"/.*; do
        name=$(basename "$item")
        # 排除当前目录和上级目录引用
        [[ "$name" == "." || "$name" == ".." ]] && continue
        [[ "$name" == ".secret" || "$name" == ".providers" ]] && continue
        cp -r "$item" "$tenant_dir/" 2>/dev/null || true
    done

    # 复制 .secret 到租户的 secret 目录
    if [ -d "$tmpdir/.secret" ]; then
        mkdir -p "$tenant_dir/.secret"
        cp -r "$tmpdir/.secret"/. "$tenant_dir/.secret/" 2>/dev/null || true
    fi

    # 复制 .providers 到 SECRET_DIR
    if [ -d "$tmpdir/.providers" ]; then
        mkdir -p "$SECRET_DIR/$tenant/providers"
        cp -r "$tmpdir/.providers"/. "$SECRET_DIR/$tenant/providers/" 2>/dev/null || true
    fi

    # 清理临时目录
    rm -rf "$tmpdir"

    echo "SUCCESS:$tenant"
    restored_count=$((restored_count + 1))
done

echo "========================================"
echo "Restored tenants: $restored_count"
echo "Rollback backups: $rollback_count"
echo "========================================"