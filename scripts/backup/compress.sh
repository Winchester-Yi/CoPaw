#!/bin/bash
# 备份压缩脚本 for CoPaw
# Usage: compress.sh [OPTIONS]
# Options:
#   --working-dir DIR     工作目录（用户数据目录）
#   --secret-dir DIR      密钥目录（providers 配置）
#   --output-dir DIR      输出目录（zip 文件存放位置）
#   --tenants LIST        指定租户（逗号分隔，空则备份所有）
#   --date DATE           备份日期 YYYY-MM-DD
#   --hour HOUR           备份小时 0-23
#   --instance-id ID      实例标识

set -euo pipefail

# 默认路径（可通过环境变量或参数覆盖）
WORKING_DIR="${SWE_BACKUP_SCRIPT_WORKING_DIR:-/opt/deployments/app/working}"
SECRET_DIR="${SWE_BACKUP_SCRIPT_SECRET_DIR:-/opt/deployments/app/working.secret}"
OUTPUT_DIR="${OUTPUT_DIR:-/tmp/backup_$(date +%Y%m%d_%H%M%S)}"
BACKUP_DATE="${BACKUP_DATE:-$(date +%Y-%m-%d)}"
BACKUP_HOUR="${BACKUP_HOUR:-$(date +%H)}"
INSTANCE_ID="${INSTANCE_ID:-default}"
TENANTS="${TENANTS:-}"

# 解析命令行参数
while [[ $# -gt 0 ]]; do
    case "$1" in
        --working-dir) WORKING_DIR="$2"; shift 2 ;;
        --secret-dir) SECRET_DIR="$2"; shift 2 ;;
        --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
        --tenants) TENANTS="$2"; shift 2 ;;
        --date) BACKUP_DATE="$2"; shift 2 ;;
        --hour) BACKUP_HOUR="$2"; shift 2 ;;
        --instance-id) INSTANCE_ID="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

echo "========================================"
echo "CoPaw Backup Compression Script"
echo "========================================"
echo "Working dir: $WORKING_DIR"
echo "Secret dir: $SECRET_DIR"
echo "Output dir: $OUTPUT_DIR"
echo "Backup date: $BACKUP_DATE"
echo "Backup hour: $BACKUP_HOUR"
echo "Instance ID: $INSTANCE_ID"
echo "========================================"

# 检查工作目录是否存在
if [[ ! -d "$WORKING_DIR" ]]; then
    echo "Error: Working directory does not exist: $WORKING_DIR" >&2
    exit 1
fi

# 创建输出目录
mkdir -p "$OUTPUT_DIR"

# 解析租户列表
TENANT_ARRAY=()
if [[ -n "$TENANTS" ]]; then
    # 将逗号分隔的租户列表转换为数组
    TENANT_ARRAY=($(echo "$TENANTS" | tr ',' ' '))
fi

# 遍历用户目录进行压缩
processed_count=0
for user_path in "$WORKING_DIR"/*; do
    user=$(basename "$user_path")

    # 跳过非目录
    [ -d "$user_path" ] || continue

    # 跳过隐藏目录
    [[ "$user" == .* ]] && continue

    # 跳过 JSON 文件（如 backup_tasks.json）
    [[ "$user" == *.json ]] && continue

    # 跳过 rollback 目录
    [[ "$user" == "rollback" ]] && continue

    # 如果指定了租户列表，只处理指定的租户
    if [[ ${#TENANT_ARRAY[@]} -gt 0 ]]; then
        skip=true
        for t in "${TENANT_ARRAY[@]}"; do
            if [[ "$user" == "$t" ]]; then
                skip=false
                break
            fi
        done
        [[ "$skip" == true ]] && continue
    fi

    # Generic recursive copying cannot guarantee a committed memory snapshot.
    if [[ -n "$(find "$user_path" \( -name state-v2.json -o -name .turn-ledger \) -print -quit)" ]]; then
        echo "Error: Versioned memory storage requires the Python backup worker with root verification." >&2
        exit 1
    fi

    echo "Processing tenant: $user"

    # 创建临时目录
    tmpdir=$(mktemp -d)

    # 复制工作目录内容（包括隐藏文件）
    cp -r "$WORKING_DIR/$user"/. "$tmpdir/" 2>/dev/null || true

    # 复制 .secret 子目录到 zip 内的 .secret 目录
    mkdir -p "$tmpdir/.secret"
    if [ -d "$WORKING_DIR/$user/.secret" ]; then
        cp -r "$WORKING_DIR/$user/.secret"/. "$tmpdir/.secret/" 2>/dev/null || true
    fi

    # 复制 providers 配置（从 SECRET_DIR）到 zip 内的 .providers 目录
    mkdir -p "$tmpdir/.providers"
    if [ -d "$SECRET_DIR/$user/providers" ]; then
        cp -r "$SECRET_DIR/$user/providers"/. "$tmpdir/.providers/" 2>/dev/null || true
    fi

    # 压缩：在临时目录内执行 zip，避免额外的目录层级
    zip_path="$OUTPUT_DIR/${user}.zip"
    (cd "$tmpdir" && zip -r "$zip_path" . >/dev/null 2>&1)

    # 清理临时目录
    rm -rf "$tmpdir"

    # 输出成功信息（供 Python 解析）
    echo "SUCCESS:$user:$zip_path"
    processed_count=$((processed_count + 1))
done

echo "========================================"
echo "Output directory: $OUTPUT_DIR"
echo "Processed tenants: $processed_count"
echo "OUTPUT_DIR:$OUTPUT_DIR"
echo "========================================"