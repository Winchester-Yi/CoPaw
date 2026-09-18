# -*- coding: utf-8 -*-
"""用户市场浏览 API 和我的技能 API."""

import asyncio
import io
import json
import logging
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import (
    APIRouter,
    Body,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ...marketplace.fs import (
    get_user_skills_dir,
    normalize_skill_name,
    _validate_skill_name_segment,
)
from ...marketplace.service import load_index
from ...marketplace.zip_download import build_skill_zip
from ...security import SkillScanError, scan_skill_directory
from ...security.skill_scanner.safe_unpack import safe_unpack_skill_zip
from ...marketplace.schemas import (
    BatchOperationRequest,
    BatchOperationResponse,
    BranchCountsResponse,
    FileContentResponse,
    FileTreeNode,
    MarketSkillDetail,
    MarketSkillResponse,
    MySkillItem,
    OperationResponse,
    ParseZipResponse,
    UploadSkillResponse,
)
from ...utils.skill_md import (
    extract_cn_name_from_title,
    extract_skill_id,
    parse_frontmatter,
)
from ...utils.skill_utils import clean_skill_name
from ...utils.logging_utils import log_params
from ..deps import decode_user_name, require_source_id

logger = logging.getLogger(__name__)

router = APIRouter()

_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB
_ALLOWED_ZIP_TYPES = {
    "application/zip",
    "application/x-zip-compressed",
    "application/octet-stream",
}


async def _flush_skill_scan_history(request: Request) -> None:
    recorder = getattr(request.app.state, "skill_scan_history_recorder", None)
    if recorder is not None:
        await recorder.flush()


async def _read_validated_zip_upload(file: UploadFile) -> bytes:
    """Validate and read uploaded zip file."""
    if file.content_type and file.content_type not in _ALLOWED_ZIP_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                "Expected a zip file, "
                f"got content-type: {file.content_type}"
            ),
        )

    data = await file.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"File too large ({len(data) // (1024 * 1024)} MB). "
                f"Maximum is {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
            ),
        )
    return data


def _decode_zip_filename(filename: str, info: zipfile.ZipInfo) -> str:
    """Decode zip filename, handling GBK encoding from Windows.

    ZIP file name encoding rules:
    - If flag_bits & 0x800: UTF-8 encoded (Python decodes correctly)
    - Otherwise: platform-specific encoding (often GBK on Chinese Windows)

    Python's zipfile module decodes non-UTF-8 filenames using cp437 by default,
    which causes Chinese characters to become garbled. We need to:
    1. Check if UTF-8 flag is set (already correct)
    2. Otherwise, reverse cp437 decoding and try GBK/UTF-8
    """
    # Check if UTF-8 flag is set (bit 11)
    if info.flag_bits & 0x800:
        return filename

    # Try to recover from cp437 mis-decoding
    try:
        raw = filename.encode("cp437")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return filename

    # Try GBK first (common on Chinese Windows)
    try:
        decoded = raw.decode("gbk")
        # Validate the result is printable
        if decoded.isprintable() or all(
            c.isprintable() or c in "\n\r\t" for c in decoded
        ):
            return decoded
    except UnicodeDecodeError:
        pass

    # Try UTF-8
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass

    return filename


_MAX_UNCOMPRESSED_SIZE = 200 * 1024 * 1024  # 200MB


def _decode_zip_member_name(info: zipfile.ZipInfo) -> str:
    """按 Market 兼容逻辑解码 ZIP 成员名."""
    return _decode_zip_filename(info.filename, info)


def _find_skill_directories(
    tmp_dir: Path,
    zip_filename: str | None = None,
) -> list[tuple[Path, str]]:
    """Find valid skill directories in extracted path."""
    real_entries = [
        path
        for path in tmp_dir.iterdir()
        if not path.name.startswith(".") and not path.name.startswith("_")
    ]

    # Handle single skill at root
    extract_root = (
        real_entries[0]
        if len(real_entries) == 1 and real_entries[0].is_dir()
        else tmp_dir
    )

    if (extract_root / "SKILL.md").exists():
        skill_name = _resolve_skill_name(extract_root, zip_filename)
        return [(extract_root, skill_name)]

    return [
        (path, _resolve_skill_name(path, zip_filename))
        for path in sorted(extract_root.iterdir())
        if not path.name.startswith(".")
        and not path.name.startswith("_")
        and path.is_dir()
        and (path / "SKILL.md").exists()
    ]


def _extract_zip_skills(
    data: bytes,
    zip_filename: str | None = None,
) -> tuple[Path, list[tuple[Path, str]]]:
    """Extract and validate a skill zip.

    Returns ``(tmp_dir, found_skills)`` where each skill is ``(skill_dir, skill_name)``.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="copaw_myskill_upload_"))

    try:
        safe_unpack_skill_zip(
            data,
            tmp_dir,
            max_uncompressed_bytes=_MAX_UNCOMPRESSED_SIZE,
            decode_member_name=_decode_zip_member_name,
        )

        found = _find_skill_directories(tmp_dir, zip_filename)
        if not found:
            raise ValueError(
                "No valid skills found in uploaded zip (missing SKILL.md)",
            )
        return tmp_dir, found
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def _scan_found_skills_or_raise(
    found_skills: list[tuple[Path, str]],
    *,
    source_id: str = "",
    user_id: str = "",
    bbk_id: str = "",
) -> None:
    """强制扫描解包后的技能目录，命中高危时阻断导入."""
    for skill_dir, skill_name in found_skills:
        scan_skill_directory(
            skill_dir,
            skill_name=skill_name,
            block=True,
            source_id=source_id,
            user_id=user_id,
            bbk_id=bbk_id,
        )


def _infer_skill_name_from_zip_filename(filename: str) -> str:
    """从 zip 文件名推导技能名（去掉 .zip 和版本号）。"""
    if not filename:
        return ""
    # 去掉 .zip 后缀
    name = filename.lower()
    if name.endswith(".zip"):
        name = name[:-4]
    # 去掉版本号后缀（如 -1.0.0, -v1.0.0）
    import re

    name = re.sub(r"-v?\d+\.\d+\.\d+$", "", name)
    name = re.sub(r"-v?\d+$", "", name)
    return name or filename


def _resolve_skill_name(
    skill_dir: Path,
    zip_filename: str | None = None,
) -> str:
    """Resolve skill name from SKILL.md frontmatter, zip filename, or directory name."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        # 尝试使用 zip 文件名
        inferred = _infer_skill_name_from_zip_filename(zip_filename or "")
        return inferred or skill_dir.name

    try:
        content = skill_md.read_text(encoding="utf-8")
    except Exception:
        inferred = _infer_skill_name_from_zip_filename(zip_filename or "")
        return inferred or skill_dir.name

    if not content.startswith("---"):
        # 没有 frontmatter，尝试使用 zip 文件名
        inferred = _infer_skill_name_from_zip_filename(zip_filename or "")
        return inferred or skill_dir.name

    # Parse YAML frontmatter
    for line in content.split("\n")[1:]:
        if line.startswith("---"):
            break
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip()
            # 去除引号（复用公共工具函数）
            name = clean_skill_name(name)
            if name:
                return name

    # frontmatter 中没有 name 字段，尝试使用 zip 文件名
    inferred = _infer_skill_name_from_zip_filename(zip_filename or "")
    return inferred or skill_dir.name


def _import_skill_dir(
    skill_dir: Path,
    skills_root: Path,
    skill_name: str,
    original_name: str,
    overwrite: bool,
) -> bool:
    """Import a skill directory to the user skills folder.

    Args:
        skill_name: 规范的目录名（normalize 后，保留中文等 Unicode 字符）
        original_name: 原始技能名称（用于 skill.json 的 name 字段）
    """
    # 验证目录名是否合法（允许中文等 Unicode 字符）
    try:
        _validate_skill_name_segment(skill_name)
    except ValueError as e:
        raise ValueError(
            f"技能目录名 '{skill_name}' 包含非法字符: {e}",
        ) from e

    target_dir = skills_root / skill_name
    if target_dir.exists() and not overwrite:
        return False

    if target_dir.exists():
        shutil.rmtree(target_dir)

    shutil.copytree(skill_dir, target_dir)
    return True


def _get_existing_skill_names(skills_dir: Path) -> set[str]:
    """Get set of existing skill directory names."""
    if not skills_dir.exists():
        return set()
    return {p.name for p in skills_dir.iterdir() if p.is_dir()}


def _parse_frontmatter_description(skill_md_path: Path) -> str:
    """从 SKILL.md frontmatter 中提取 description（委托共享工具）."""
    from ...utils.skill_md import extract_metadata

    if not skill_md_path.exists():
        return ""
    try:
        content = skill_md_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return extract_metadata(content).get("description", "")


def _parse_frontmatter_version(skill_md_path: Path) -> str:
    """从 SKILL.md frontmatter 中提取 version（委托共享工具）."""
    from ...utils.skill_md import extract_version

    if not skill_md_path.exists():
        return ""
    try:
        return extract_version(skill_md_path.read_text(encoding="utf-8"))
    except OSError:
        return ""


def _bump_patch_version(version: str) -> str:
    """递增版本号的 patch 部分（委托共享工具）."""
    from ...utils.version import bump_patch

    return bump_patch(version)


def _build_skill_metadata(
    skill_dir: Path,
    skill_name: str,
    original_name: str,
    existing_version: str = "",
    cn_name: str = "",
    source_id: str = "",
    user_id: str = "",
) -> dict[str, Any]:
    """构建技能元数据（用于写入 manifest），包含版本处理.

    版本策略（与市场一致）：
    1. SKILL.md 有版本 → 使用该版本（用户声明）
    2. SKILL.md 无版本 → 新技能用 1.0.0，覆盖时 bump

    Args:
        skill_dir: 技能目录
        skill_name: 安全的目录名
        original_name: 原始技能名称（用于前端展示）
        existing_version: 已存在技能的版本号（用于 bump）
        cn_name: 用户手动输入的中文展示名（优先级最高）
        source_id: 来源 ID（用于生成 skill_id）

    Returns:
        技能元数据字典
    """
    skill_json_path = skill_dir / "skill.json"
    skill_data: dict[str, Any] = {}

    # 读取已有的 skill.json（如果存在）
    if skill_json_path.exists():
        try:
            skill_data = json.loads(
                skill_json_path.read_text(encoding="utf-8"),
            )
        except (json.JSONDecodeError, OSError):
            pass

    # name 字段优先使用用户指定的名称（original_name），其次保留已有名称
    skill_data["name"] = original_name or skill_data.get("name") or skill_name

    # 版本处理（与市场一致）
    skill_md_path = skill_dir / "SKILL.md"
    version_from_md = _parse_frontmatter_version(skill_md_path)
    if version_from_md:
        # SKILL.md 有版本 → 使用用户声明的版本
        skill_data["version"] = version_from_md
    elif existing_version:
        # SKILL.md 无版本，已有技能 → bump 版本
        skill_data["version"] = _bump_patch_version(existing_version)
    else:
        # SKILL.md 无版本，新技能 → 默认 1.0.0
        skill_data.setdefault("version", "1.0.0")

    # 优先从 skill.json 获取 description，其次从 SKILL.md frontmatter
    if not skill_data.get("description"):
        desc_from_md = _parse_frontmatter_description(skill_md_path)
        if desc_from_md:
            skill_data["description"] = desc_from_md
        else:
            skill_data.setdefault("description", "")

    skill_data["source"] = skill_data.get("source", "customized")

    # 时间字段处理
    current_time = datetime.now(timezone.utc).isoformat()
    if not skill_data.get("created_at"):
        skill_data["created_at"] = current_time
    else:
        skill_data["updated_at"] = current_time

    # skill_id 和 cn_name 处理
    md_content = ""
    if skill_md_path.exists():
        try:
            md_content = skill_md_path.read_text(encoding="utf-8")
        except OSError:
            pass

    # skill_id：优先 metadata.skill_id，其次自动生成
    # "我的技能"场景：source 固定使用 "customized"，生成格式 customized_{user_id}_{skill_name}
    skill_id = extract_skill_id(
        md_content,
        "customized",
        skill_name,
        creator_id=user_id,
    )
    skill_data["skill_id"] = skill_id
    skill_data["source"] = "customized"

    # cn_name：优先用户输入，其次 metadata.cn_name，再次一级标题，最后 skill_name fallback
    resolved_cn_name = cn_name.strip() if cn_name else ""
    if not resolved_cn_name:
        resolved_cn_name = skill_data.get("cn_name") or ""
    if not resolved_cn_name and md_content:
        resolved_cn_name = extract_cn_name_from_title(md_content)
    if not resolved_cn_name:
        resolved_cn_name = skill_name
    skill_data["cn_name"] = resolved_cn_name

    return skill_data


def _process_single_skill(
    skill_dir: Path,
    skills_dir: Path,
    skill_name: str,
    original_name: str,
    existing_names: set[str],
    user_id: str,
    user_name: str,
    bbk_id: str,
    overwrite: bool,
    category_id: int | None,
    cn_name: str = "",
    source_id: str = "",
) -> tuple[bool, dict[str, str] | None, dict[str, Any] | None]:
    """Process single skill import. Returns (imported, conflict_or_none, metadata).

    Args:
        skill_name: 安全的目录名
        original_name: 原始技能名称
        source_id: 来源 ID（用于生成 skill_id）

    Returns:
        (是否导入成功, 冲突信息, 技能元数据用于写入 manifest)
    """
    # 获取已存在技能的版本（用于覆盖时 bump）
    existing_version = ""
    if skill_name in existing_names:
        existing_skill_json = skills_dir / skill_name / "skill.json"
        if existing_skill_json.exists():
            try:
                existing_data = json.loads(
                    existing_skill_json.read_text(encoding="utf-8"),
                )
                existing_version = existing_data.get("version", "")
            except (json.JSONDecodeError, OSError):
                pass

    if skill_name in existing_names and not overwrite:
        # 递增计数器直到找到不冲突的建议名
        counter = 1
        while True:
            suggested = f"{original_name}_{counter}"
            safe_suggested = normalize_skill_name(suggested)
            if safe_suggested not in existing_names:
                break
            counter += 1
        return (
            False,
            {
                "reason": "already_exists",
                "skill_name": skill_name,
                "original_name": original_name,
                "suggested_name": suggested,
            },
            None,
        )

    if not _import_skill_dir(
        skill_dir,
        skills_dir,
        skill_name,
        original_name,
        overwrite,
    ):
        return False, None, None

    # 构建技能元数据（包含版本处理）
    imported_skill_dir = skills_dir / skill_name
    skill_metadata = _build_skill_metadata(
        imported_skill_dir,
        skill_name,
        original_name,
        existing_version=existing_version,
        cn_name=cn_name,
        source_id=source_id,
        user_id=user_id,
    )

    # 添加上传者信息到元数据
    skill_metadata["creator_id"] = user_id
    skill_metadata["creator_name"] = user_name
    skill_metadata["bbk_id"] = bbk_id
    if category_id is not None:
        skill_metadata["category_id"] = category_id

    return True, None, skill_metadata


def _import_skill_from_zip(
    skills_dir: Path,
    data: bytes,
    user_id: str,
    user_name: str,
    bbk_id: str,
    overwrite: bool = False,
    target_name: str = "",
    rename_map: dict[str, str] | None = None,
    category_id: int | None = None,
    zip_filename: str | None = None,
    cn_name: str = "",
    source_id: str = "",
) -> dict[str, Any]:
    """Import skill from zip data to user skills directory."""
    imported: list[str] = []
    conflicts: list[dict[str, str]] = []
    skills_metadata: dict[str, dict[str, Any]] = {}  # skill_name -> metadata
    tmp_dir: Path | None = None
    parsed_name: str | None = None
    parsed_description: str | None = None

    try:
        tmp_dir, found_skills = _extract_zip_skills(data, zip_filename)
        _scan_found_skills_or_raise(
            found_skills,
            source_id=source_id,
            user_id=user_id,
            bbk_id=bbk_id,
        )
        existing_names = _get_existing_skill_names(skills_dir)

        for skill_dir, original_name in found_skills:
            # original_name 来自 SKILL.md frontmatter 或 zip 文件名
            # 将原始名称规范化为目录名（保留中文等 Unicode 字符）
            safe_skill_name = normalize_skill_name(original_name)

            # 应用 rename_map 映射（用户手动指定的重命名）
            # 需要传递解析：rename_map 可能包含链式映射
            # 如 {A→B, B→C}，表示最终要将 A 重命名为 C
            if rename_map and original_name in rename_map:
                resolved = original_name
                seen = {resolved}
                while resolved in rename_map:
                    resolved = rename_map[resolved]
                    if resolved in seen:
                        break  # 防止循环引用
                    seen.add(resolved)
                original_name = resolved
                safe_skill_name = normalize_skill_name(original_name)
            elif target_name and len(found_skills) == 1:
                safe_skill_name = normalize_skill_name(target_name.strip())

            success, conflict, metadata = _process_single_skill(
                skill_dir,
                skills_dir,
                safe_skill_name,
                original_name,
                existing_names,
                user_id,
                user_name,
                bbk_id,
                overwrite,
                category_id,
                cn_name,
                source_id,
            )

            if conflict:
                conflicts.append(conflict)
                continue

            if success and metadata:
                imported.append(safe_skill_name)
                skills_metadata[safe_skill_name] = metadata
                if parsed_name is None:
                    parsed_name = metadata.get("name")
                    parsed_description = metadata.get("description")

    except zipfile.BadZipFile as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid zip file: {e}",
        ) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except SkillScanError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        if tmp_dir and tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)

    result = {
        "imported": imported,
        "count": len(imported),
        "name": parsed_name,
        "description": parsed_description,
        "skills_metadata": skills_metadata,  # 返回 metadata 用于写入 manifest
    }
    if conflicts:
        result["conflicts"] = conflicts
    return result


@router.get("/market/bbk-ids", response_model=BranchCountsResponse)
async def list_bbk_ids(
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_bbk_id: Optional[str] = Header(default=None, alias="X-Bbk-Id"),
):
    """获取所有有数据的分行 ID 列表及技能/MCP 数量（用于前端分行菜单固定渲染）。

    总行用户(X-Bbk-Id=100): 返回所有有数据的分行
    分行用户: 返回总行(100) + 当前分行
    """
    source_id = require_source_id(x_source_id)
    svc = request.app.state.marketplace
    is_head_office = x_bbk_id == "100"
    visible_category_ids = None
    if not is_head_office:
        if not svc.db.is_connected:
            raise HTTPException(status_code=503, detail="Database unavailable")
        category_rows = await svc.db.fetch_all(
            "SELECT id FROM swe_marketplace_categories "
            "WHERE source_id = %s AND COALESCE(branch_visible, 1) = 1",
            (source_id,),
        )
        visible_category_ids = {int(row["id"]) for row in category_rows}
    all_branches = svc.list_all_bbk_ids(
        source_id,
        visible_category_ids=visible_category_ids,
    )

    # 总行用户: 返回所有分行
    if is_head_office:
        return BranchCountsResponse(branches=all_branches)

    # 分行用户: 只返回总行(100) + 当前分行
    branch_ids = {"100"}
    if x_bbk_id and x_bbk_id != "100":
        branch_ids.add(x_bbk_id)

    filtered = [b for b in all_branches if b["bbk_id"] in branch_ids]
    return BranchCountsResponse(branches=filtered)


@router.get("/market/skills", response_model=list[MarketSkillResponse])
async def list_skills(
    request: Request,
    category_id: Optional[int] = None,
    bbk_ids: Optional[str] = Query(
        default=None,
        description="逗号分隔的分行ID列表",
    ),
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_bbk_id: Optional[str] = Header(default=None, alias="X-Bbk-Id"),
):
    """浏览市场技能列表（按 source_id + category_id + bbk_ids 过滤）。"""
    source_id = require_source_id(x_source_id)
    user_bbk_id = x_bbk_id or "100"
    is_head_office = x_bbk_id == "100"

    log_params(
        logger,
        request.method,
        request.url.path,
        category_id=category_id,
        bbk_ids=bbk_ids,
    )

    # 解析 bbk_ids 参数（逗号分隔）
    parsed_bbk_ids = None
    if bbk_ids:
        parsed_bbk_ids = [b.strip() for b in bbk_ids.split(",") if b.strip()]
    svc = request.app.state.marketplace
    visible_category_ids = None
    if not is_head_office:
        if not svc.db.is_connected:
            raise HTTPException(status_code=503, detail="Database unavailable")
        category_rows = await svc.db.fetch_all(
            "SELECT id FROM swe_marketplace_categories "
            "WHERE source_id = %s AND COALESCE(branch_visible, 1) = 1",
            (source_id,),
        )
        visible_category_ids = {int(row["id"]) for row in category_rows}
    return await svc.list_skills(
        source_id,
        user_bbk_id,
        category_id=category_id,
        bbk_ids=parsed_bbk_ids,
        is_manager=is_head_office,
        visible_category_ids=visible_category_ids,
    )


@router.get("/market/skills/mine", response_model=list[MySkillItem])
async def get_my_skills(
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    agent_id: str = "default",
):
    """我创建的技能列表."""
    source_id = require_source_id(x_source_id)
    if not x_user_id:
        raise HTTPException(
            status_code=400,
            detail="X-User-Id header is required",
        )
    svc = request.app.state.marketplace

    log_params(
        logger,
        request.method,
        request.url.path,
        source_id=source_id,
        user_id=x_user_id,
        agent_id=agent_id,
    )
    all_skills = await svc.get_my_skills(source_id, x_user_id, agent_id)
    return [s for s in all_skills if not s.is_received]


@router.get("/market/skills/received", response_model=list[MySkillItem])
async def get_received_skills(
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    agent_id: str = "default",
):
    """我接收的技能列表."""
    source_id = require_source_id(x_source_id)
    if not x_user_id:
        raise HTTPException(
            status_code=400,
            detail="X-User-Id header is required",
        )
    svc = request.app.state.marketplace

    log_params(
        logger,
        request.method,
        request.url.path,
        source_id=source_id,
        user_id=x_user_id,
        agent_id=agent_id,
    )
    all_skills = await svc.get_my_skills(source_id, x_user_id, agent_id)
    return [s for s in all_skills if s.is_received]


@router.get("/market/skills/{item_id}", response_model=MarketSkillDetail)
async def get_skill_detail(
    item_id: str,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_bbk_id: Optional[str] = Header(default=None, alias="X-Bbk-Id"),
):
    """预览技能详情."""
    source_id = require_source_id(x_source_id)
    user_bbk_id = x_bbk_id or "100"

    log_params(logger, request.method, request.url.path, item_id=item_id)

    svc = request.app.state.marketplace
    is_head_office = x_bbk_id == "100"
    visible_category_ids = None
    if not is_head_office and svc.db.is_connected:
        category_rows = await svc.db.fetch_all(
            "SELECT id FROM swe_marketplace_categories "
            "WHERE source_id = %s AND COALESCE(branch_visible, 1) = 1",
            (source_id,),
        )
        visible_category_ids = {int(row["id"]) for row in category_rows}
    detail = await svc.get_skill_detail(
        source_id,
        item_id,
        user_bbk_id,
        is_manager=is_head_office,
        visible_category_ids=visible_category_ids,
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return detail


@router.get(
    "/market/skills/{item_id}/files",
    response_model=list[FileTreeNode],
)
async def list_market_skill_files(
    item_id: str,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_bbk_id: Optional[str] = Header(default=None, alias="X-Bbk-Id"),
):
    """获取市场技能详情页文件树。"""
    source_id = require_source_id(x_source_id)
    user_bbk_id = x_bbk_id or "100"

    log_params(logger, request.method, request.url.path, item_id=item_id)

    svc = request.app.state.marketplace
    files = svc.list_market_skill_files(source_id, item_id, user_bbk_id)
    if files is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return files


@router.get(
    "/market/skills/{item_id}/files/{file_path:path}",
    response_model=FileContentResponse,
)
async def read_market_skill_file(
    item_id: str,
    file_path: str,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_bbk_id: Optional[str] = Header(default=None, alias="X-Bbk-Id"),
):
    """读取市场技能详情页文件内容。"""
    source_id = require_source_id(x_source_id)
    user_bbk_id = x_bbk_id or "100"

    log_params(
        logger,
        request.method,
        request.url.path,
        item_id=item_id,
        file_path=file_path,
    )

    svc = request.app.state.marketplace
    content, file_type = svc.read_market_skill_file(
        source_id,
        item_id,
        file_path,
        user_bbk_id,
    )
    if file_type == "binary":
        return FileContentResponse(content="", file_type=file_type)
    if content is None:
        raise HTTPException(status_code=404, detail="File not found")
    return FileContentResponse(content=content, file_type=file_type)


async def _check_skill_name_exists_market(
    svc,
    source_id: str,
    safe_skill_name: str,
) -> tuple[bool, str]:
    """应用市场场景：检查市场索引中是否有同名技能.

    Returns:
        (exists, existing_skill_id)
    """
    items = load_index(svc.marketplace_root, source_id)
    existing = next(
        (
            i
            for i in items
            if i.name == safe_skill_name and i.item_type == "skill"
        ),
        None,
    )
    if existing:
        return True, existing.skill_id or ""
    return False, ""


def _check_skill_name_exists_user(
    swe_root: Path,
    user_id: str,
    agent_id: str,
    source_id: str,
    safe_skill_name: str,
) -> bool:
    """用户场景：检查用户目录中是否有同名技能."""
    skills_dir = get_user_skills_dir(swe_root, user_id, agent_id, source_id)
    existing_names = _get_existing_skill_names(skills_dir)
    return safe_skill_name in existing_names


async def _check_skill_id_conflict_market(
    svc,
    skill_id: str,
    safe_skill_name: str,
) -> tuple[int, list[str]]:
    """应用市场场景：检查 skill_id 冲突.

    Returns:
        (used_count, used_by_list)
    """
    if not skill_id or not svc.db.is_connected:
        return 0, []

    try:
        rows = await svc.db.fetch_all(
            """
            SELECT DISTINCT skill_name, cn_name, tenant_name, tenant_id FROM swe_skills
            WHERE skill_id = %s AND skill_name != %s
            LIMIT 10
            """,
            (skill_id, safe_skill_name),
        )
        if not rows:
            return 0, []

        count_row = await svc.db.fetch_one(
            """
            SELECT COUNT(DISTINCT skill_name) as cnt FROM swe_skills
            WHERE skill_id = %s AND skill_name != %s
            """,
            (skill_id, safe_skill_name),
        )
        used_count = (
            count_row.get("cnt", len(rows)) if count_row else len(rows)
        )

        used_by: list[str] = []
        for r in rows[:3]:
            display_name = r.get("cn_name") or r.get("skill_name", "")
            used_by.append(display_name)
        for r in rows[:3]:
            user_name = r.get("tenant_name", "") or r.get("tenant_id", "")
            used_by.append(user_name)

        return used_count, used_by
    except Exception as e:
        logger.warning("Failed to check skill_id conflict: %s", e)
        return 0, []


async def _check_skill_id_conflict_user(
    svc,
    skill_id: str,
    safe_skill_name: str,
    user_id: str,
) -> Optional[str]:
    """用户场景：检查 skill_id 冲突.

    Returns:
        冲突信息字符串，无冲突返回 None
    """
    if not skill_id or not svc.db.is_connected:
        return None

    try:
        row = await svc.db.fetch_one(
            """
            SELECT skill_name, cn_name FROM swe_skills
            WHERE skill_id = %s AND tenant_id = %s
            """,
            (skill_id, user_id),
        )
        if not row:
            return None

        existing_skill_name = row.get("skill_name", "")
        if existing_skill_name == safe_skill_name:
            return None  # 同技能名，视为覆盖操作

        existing_cn_name = row.get("cn_name", "")
        conflict_display = existing_cn_name or existing_skill_name
        return f"skill_id '{skill_id}' 已被技能 '{conflict_display}' 占用"
    except Exception as e:
        logger.warning("Failed to check skill_id conflict: %s", e)
        return None


@router.post("/market/skills/parse-zip", response_model=ParseZipResponse)
async def parse_skill_zip(
    request: Request,
    file: UploadFile = File(..., description="Skill zip file to parse"),
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    market_mode: bool = Query(
        False,
        description="应用市场模式：检查跨租户 skill_id 冲突",
    ),
):
    """解析 zip 文件中的 SKILL.md，返回 skill_name、cn_name、skill_id 等预览信息.

    同时检查用户技能目录中是否已存在同名技能。
    market_mode=true 时检查跨租户 skill_id 冲突（应用市场场景）。
    """
    source_id = require_source_id(x_source_id)
    # 应用市场模式不需要 x_user_id
    if not market_mode and not x_user_id:
        raise HTTPException(
            status_code=400,
            detail="X-User-Id header is required",
        )

    log_params(
        logger,
        request.method,
        request.url.path,
        market_mode=market_mode,
        file_size=file.size,
    )

    svc = request.app.state.marketplace
    swe_root = svc.swe_root
    agent_id = "default"

    # Read and validate zip
    data = await _read_validated_zip_upload(file)

    try:
        tmp_dir, found_skills = await asyncio.to_thread(
            _extract_zip_skills,
            data,
            file.filename,
        )
        if not found_skills:
            return ParseZipResponse(
                error="No valid skills found (missing SKILL.md)",
            )

        # 只解析第一个技能
        skill_dir, skill_name = found_skills[0]
        skill_md_path = skill_dir / "SKILL.md"

        md_content = ""
        if skill_md_path.exists():
            try:
                md_content = skill_md_path.read_text(encoding="utf-8")
            except OSError:
                pass

        # 提取预览元数据：cn_name、skill_id、description
        cn_name, skill_id, description = _extract_skill_preview_metadata(
            skill_dir,
            skill_name,
            md_content,
            market_mode,
            x_user_id,
        )

        # 判重校验：使用 normalize_skill_name 获取实际目录名，检查是否已存在
        safe_skill_name = normalize_skill_name(skill_name)
        (
            exists,
            existing_skill_id,
            skill_id_conflict,
            skill_id_used_count,
            skill_id_used_by,
        ) = await _check_skill_duplicates_and_conflicts(
            svc,
            market_mode,
            source_id,
            x_user_id,
            swe_root,
            agent_id,
            safe_skill_name,
            skill_id,
        )

        # 市场模式下，同名技能存在时复用已有 skill_id
        skill_id_reused = False
        if market_mode and exists and existing_skill_id:
            skill_id = existing_skill_id
            skill_id_reused = True

        # 清理临时目录
        if tmp_dir and tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)

        return ParseZipResponse(
            skill_name=skill_name,
            cn_name=cn_name,
            skill_id=skill_id,
            description=description,
            exists=exists,
            skill_id_reused=skill_id_reused,
            skill_id_conflict=skill_id_conflict,
            skill_id_used_count=skill_id_used_count,
            skill_id_used_by=skill_id_used_by,
        )

    except ValueError as e:
        return ParseZipResponse(error=str(e))
    except Exception as e:
        logger.warning("Failed to parse zip: %s", e)
        return ParseZipResponse(error=f"Failed to parse zip: {e}")


def _extract_skill_preview_metadata(
    skill_dir: Path,
    skill_name: str,
    md_content: str,
    market_mode: bool,
    x_user_id: Optional[str],
) -> tuple[str, str, str]:
    """从 SKILL.md 提取预览元数据.

    Args:
        skill_dir: 技能目录
        skill_name: 技能名称
        md_content: SKILL.md 内容
        market_mode: 是否市场模式
        x_user_id: 用户 ID（非市场模式必填）

    Returns:
        (cn_name, skill_id, description)
    """
    # 提取 cn_name
    cn_name = ""
    fm = parse_frontmatter(md_content) if md_content else {}
    metadata = fm.get("metadata", {})
    if isinstance(metadata, dict):
        cn_name = metadata.get("cn_name", "")
    if not cn_name:
        cn_name = extract_cn_name_from_title(md_content)
    if not cn_name:
        cn_name = skill_name

    # 提取 skill_id
    if market_mode:
        # 市场场景：优先 metadata.skill_id，否则自动生成
        skill_id = ""
        if isinstance(metadata, dict):
            skill_id = metadata.get("skill_id", "") or ""
        if not skill_id:
            # 自动生成唯一标识：skill_{uuid[:8]}
            import uuid

            skill_id = f"skill_{uuid.uuid4().hex[:8]}"
    else:
        # 我的技能场景：source 使用 "customized"
        assert x_user_id is not None
        skill_id = extract_skill_id(
            md_content,
            "customized",
            skill_name,
            creator_id=x_user_id,
        )

    # 提取 description
    description = fm.get("description", "") or ""

    return cn_name, skill_id, description


async def _check_skill_duplicates_and_conflicts(
    svc,
    market_mode: bool,
    source_id: str,
    x_user_id: Optional[str],
    swe_root: Path,
    agent_id: str,
    safe_skill_name: str,
    skill_id: str,
) -> tuple[bool, str, Optional[str], int, list[str]]:
    """检查技能判重和 skill_id 冲突.

    Args:
        svc: Marketplace 服务
        market_mode: 是否市场模式
        source_id: 租户 ID
        x_user_id: 用户 ID
        swe_root: SWE 根目录
        agent_id: Agent ID
        safe_skill_name: 安全技能名
        skill_id: 技能 ID

    Returns:
        (exists, existing_skill_id, skill_id_conflict, skill_id_used_count, skill_id_used_by)
        skill_id_conflict 为 Optional[str]，表示冲突信息字符串
    """
    exists = False
    existing_skill_id = ""
    skill_id_conflict = None
    skill_id_used_count = 0
    skill_id_used_by: list[str] = []

    if market_mode:
        exists, existing_skill_id = await _check_skill_name_exists_market(
            svc,
            source_id,
            safe_skill_name,
        )
        skill_id_used_count, skill_id_used_by = (
            await _check_skill_id_conflict_market(
                svc,
                skill_id,
                safe_skill_name,
            )
        )
    elif x_user_id:
        exists = _check_skill_name_exists_user(
            swe_root,
            x_user_id,
            agent_id,
            source_id,
            safe_skill_name,
        )
        skill_id_conflict = await _check_skill_id_conflict_user(
            svc,
            skill_id,
            safe_skill_name,
            x_user_id,
        )

    return (
        exists,
        existing_skill_id,
        skill_id_conflict,
        skill_id_used_count,
        skill_id_used_by,
    )


async def _log_upload_operation(
    svc,
    source_id: str,
    user_id: str,
    user_name: str,
    bbk_id: str,
    imported_skills: list[str],
) -> None:
    """记录上传操作日志."""
    if not svc.db.is_connected or not imported_skills:
        return
    try:
        await svc.db.execute(
            """
            INSERT INTO swe_user_item_operation_logs
                (source_id, user_id, user_name, bbk_id, operation,
                 item_type, item_name)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                source_id,
                user_id,
                user_name,
                bbk_id,
                "upload",
                "skill",
                ",".join(imported_skills),
            ),
        )
    except Exception as e:
        logger.warning("Failed to log upload operation: %s", e)


async def _check_skill_id_conflict(
    svc,
    skill_id: str,
    skill_name: str,
    user_id: str,
) -> None:
    """检查 skill_id 冲突，冲突时抛出 HTTPException."""
    if not skill_id or not svc.db.is_connected:
        return

    existing_row = await svc.db.fetch_one(
        """
        SELECT skill_name, cn_name FROM swe_skills
        WHERE skill_id = %s AND tenant_id = %s
        """,
        (skill_id, user_id),
    )
    if existing_row:
        existing_skill_name = existing_row.get("skill_name", "")
        if existing_skill_name != skill_name:
            existing_cn_name = existing_row.get("cn_name", "")
            conflict_display = existing_cn_name or existing_skill_name
            raise HTTPException(
                status_code=400,
                detail=(
                    f"skill_id '{skill_id}' 已被技能 '{conflict_display}' 占用，"
                    "请修改 SKILL.md 的 metadata.skill_id"
                ),
            )


async def _register_uploaded_skill_to_db(
    svc,
    skill_name: str,
    skill_metadata: dict,
    user_id: str,
    user_name: str,
    bbk_id: str,
    source_id: str,
    enabled: bool,
) -> None:
    """注册技能到数据库."""
    skill_id = skill_metadata.get("skill_id", "")
    cn_name_val = skill_metadata.get("cn_name", skill_name)
    version_text = skill_metadata.get("version", "1.0.0")
    description = skill_metadata.get("description", "")

    await _check_skill_id_conflict(svc, skill_id, skill_name, user_id)

    await svc.skill_registry.insert_skill(
        skill_id=skill_id,
        skill_name=skill_name,
        cn_name=cn_name_val,
        tenant_id=user_id,
        tenant_name=user_name,
        bbk_id=bbk_id,
        source="customized",
        source_id=source_id,
        enabled=enabled,
        description=description,
        version_text=version_text,
    )


@router.post("/market/skills/upload", response_model=UploadSkillResponse)
async def upload_skill_to_workspace(
    request: Request,
    file: UploadFile = File(..., description="Skill zip file to upload"),
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_user_name: Optional[str] = Header(default=None, alias="X-User-Name"),
    x_bbk_id: Optional[str] = Header(default=None, alias="X-Bbk-Id"),
    enable: bool = True,
    overwrite: bool = False,
    target_name: str = "",
    rename_map: str = "",
    category_id: Optional[int] = None,
    cn_name: str = "",
):
    """上传技能到工作区，记录 user_id, bbk_id, user_name。可选指定分类。"""
    source_id = require_source_id(x_source_id)
    if not x_user_id:
        raise HTTPException(
            status_code=400,
            detail="X-User-Id header is required",
        )

    log_params(
        logger,
        request.method,
        request.url.path,
        enable=enable,
        overwrite=overwrite,
        target_name=target_name,
        rename_map=rename_map,
        category_id=category_id,
        cn_name=cn_name,
        file_size=file.size,
    )

    # 解析 rename_map JSON
    parsed_rename_map: dict[str, str] = {}
    if rename_map:
        try:
            parsed_rename_map = json.loads(rename_map)
        except json.JSONDecodeError:
            logger.warning("Invalid rename_map JSON: %s", rename_map)

    svc = request.app.state.marketplace
    swe_root = svc.swe_root
    user_name = decode_user_name(x_user_name) or x_user_id
    bbk_id = x_bbk_id or "100"
    agent_id = "default"

    # 通过统一 scope_id 定位用户技能目录，避免跨 source 共享本地状态。
    skills_dir = get_user_skills_dir(swe_root, x_user_id, agent_id, source_id)
    skills_dir.mkdir(parents=True, exist_ok=True)

    # Read and validate zip
    data = await _read_validated_zip_upload(file)

    # Import skill
    try:
        result = await asyncio.to_thread(
            _import_skill_from_zip,
            skills_dir,
            data,
            x_user_id,
            user_name,
            bbk_id,
            overwrite=overwrite,
            target_name=target_name,
            rename_map=parsed_rename_map,
            category_id=category_id,
            zip_filename=file.filename,
            cn_name=cn_name,
            source_id=source_id,
        )
    except (SkillScanError, HTTPException):
        await _flush_skill_scan_history(request)
        raise

    # Log upload operation
    imported_skills = result.get("imported") or []
    await _log_upload_operation(
        svc,
        source_id,
        x_user_id,
        user_name,
        bbk_id,
        imported_skills,
    )

    # 注册技能到 manifest 和数据库
    if result.get("imported"):
        skills_metadata = result.get("skills_metadata") or {}
        for skill_name in result["imported"]:
            skill_metadata = skills_metadata.get(skill_name) or {}
            svc.register_skill_in_manifest(
                x_user_id,
                skill_name,
                agent_id,
                source_id,
                enabled=enable,
                source="customized",
                extra_metadata=skill_metadata,
            )

            await _register_uploaded_skill_to_db(
                svc,
                skill_name,
                skill_metadata,
                x_user_id,
                user_name,
                bbk_id,
                source_id,
                enable,
            )

    # 移除 skills_metadata，不返回给前端
    skills_metadata = result.pop("skills_metadata", None)
    result["enabled"] = enable

    # 提取第一个导入技能的 skill_id 和 cn_name 用于展示
    imported_list = result.get("imported") or []
    if imported_list and skills_metadata:
        first_skill_name = imported_list[0]
        first_metadata = skills_metadata.get(first_skill_name) or {}
        result["skill_id"] = first_metadata.get("skill_id")
        result["cn_name"] = first_metadata.get("cn_name")

    return result


@router.get(
    "/market/skills/mine/{skill_name}/files",
    response_model=list[FileTreeNode],
)
async def list_skill_files(
    skill_name: str,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    agent_id: str = "default",
):
    """获取技能文件树."""
    source_id = require_source_id(x_source_id)
    if not x_user_id:
        raise HTTPException(
            status_code=400,
            detail="X-User-Id header is required",
        )

    log_params(
        logger,
        request.method,
        request.url.path,
        skill_name=skill_name,
        agent_id=agent_id,
    )

    svc = request.app.state.marketplace
    return svc.list_skill_files(x_user_id, skill_name, agent_id, source_id)


@router.get("/market/skills/mine/{skill_name}/download")
async def download_my_skill(
    skill_name: str,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    agent_id: str = "default",
):
    """下载我创建的技能 ZIP。"""
    source_id = require_source_id(x_source_id)
    if not x_user_id:
        raise HTTPException(
            status_code=400,
            detail="X-User-Id header is required",
        )

    log_params(
        logger,
        request.method,
        request.url.path,
        skill_name=skill_name,
        agent_id=agent_id,
    )

    svc = request.app.state.marketplace
    skills = await svc.get_my_skills(source_id, x_user_id, agent_id)
    skill = next(
        (
            item
            for item in skills
            if item.skill_name == skill_name and not item.is_received
        ),
        None,
    )
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")

    skill_dir = svc.get_registered_skill_dir(
        x_user_id,
        skill_name,
        agent_id,
        source_id,
    )
    if skill_dir is None or not skill_dir.is_dir():
        raise HTTPException(status_code=404, detail="Skill not found")

    with tempfile.TemporaryDirectory(
        prefix="copaw_skill_download_",
    ) as temp_dir:
        zip_path = await asyncio.to_thread(
            build_skill_zip,
            skill_dir,
            f"{skill_name}.zip",
            Path(temp_dir),
        )
        zip_bytes = await asyncio.to_thread(zip_path.read_bytes)

    # RFC 6266: 使用 filename* 参数支持 UTF-8 中文文件名
    from urllib.parse import quote

    encoded_filename = quote(zip_path.name, safe="")
    content_disposition = f"attachment; filename=\"skill.zip\"; filename*=UTF-8''{encoded_filename}"

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": content_disposition,
        },
    )


@router.get("/market/skills/{item_id}/download")
async def download_market_skill(
    item_id: str,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_bbk_id: Optional[str] = Header(default=None, alias="X-Bbk-Id"),
):
    """下载市场当前版本技能 ZIP。"""
    source_id = require_source_id(x_source_id)
    user_bbk_id = x_bbk_id or "100"

    log_params(logger, request.method, request.url.path, item_id=item_id)

    svc = request.app.state.marketplace

    detail = await svc.get_skill_detail(source_id, item_id, user_bbk_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Skill not found")

    skill_dir = svc.marketplace_root / source_id / "skills" / item_id
    if not skill_dir.is_dir():
        raise HTTPException(status_code=404, detail="Skill not found")

    with tempfile.TemporaryDirectory(
        prefix="copaw_market_skill_download_",
    ) as temp_dir:
        zip_path = await asyncio.to_thread(
            build_skill_zip,
            skill_dir,
            f"{detail.name}-{detail.version}.zip",
            Path(temp_dir),
        )
        zip_bytes = await asyncio.to_thread(zip_path.read_bytes)

    # RFC 6266: 使用 filename* 参数支持 UTF-8 中文文件名
    from urllib.parse import quote

    encoded_filename = quote(zip_path.name, safe="")
    content_disposition = f"attachment; filename=\"skill.zip\"; filename*=UTF-8''{encoded_filename}"

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": content_disposition,
        },
    )


@router.get(
    "/market/skills/mine/{skill_name}/files/{file_path:path}",
    response_model=FileContentResponse,
)
async def read_skill_file(
    skill_name: str,
    file_path: str,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    agent_id: str = "default",
):
    """读取技能文件内容."""
    source_id = require_source_id(x_source_id)
    if not x_user_id:
        raise HTTPException(
            status_code=400,
            detail="X-User-Id header is required",
        )

    log_params(
        logger,
        request.method,
        request.url.path,
        skill_name=skill_name,
        file_path=file_path,
        agent_id=agent_id,
    )

    svc = request.app.state.marketplace
    content, file_type = svc.read_skill_file(
        x_user_id,
        skill_name,
        file_path,
        agent_id,
        source_id,
    )
    if content is None:
        if file_type == "binary":
            raise HTTPException(
                status_code=400,
                detail="Binary file not supported for preview",
            )
        raise HTTPException(status_code=404, detail="File not found")
    return FileContentResponse(content=content, file_type=file_type)


@router.put(
    "/market/skills/mine/{skill_name}/files/{file_path:path}",
    response_model=OperationResponse,
)
async def save_skill_file(
    skill_name: str,
    file_path: str,
    request: Request,
    content: str = Body(..., embed=True),
    cn_name: Optional[str] = Body(default=None, embed=True),
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_user_name: Optional[str] = Header(default=None, alias="X-User-Name"),
    x_bbk_id: Optional[str] = Header(default=None, alias="X-Bbk-Id"),
    agent_id: str = "default",
):
    """保存技能文件内容，可选更新中文名（仅我创建的技能支持）."""
    source_id = require_source_id(x_source_id)
    if not x_user_id:
        raise HTTPException(
            status_code=400,
            detail="X-User-Id header is required",
        )

    log_params(
        logger,
        request.method,
        request.url.path,
        skill_name=skill_name,
        file_path=file_path,
        agent_id=agent_id,
    )

    svc = request.app.state.marketplace
    skills = await svc.get_my_skills(source_id, x_user_id, agent_id)
    skill = next((s for s in skills if s.skill_name == skill_name), None)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    if skill.is_received:
        raise HTTPException(
            status_code=403,
            detail="Only created skills can be edited",
        )

    ok, new_version = svc.save_skill_file(
        x_user_id,
        skill_name,
        file_path,
        content,
        user_name=decode_user_name(x_user_name),
        agent_id=agent_id,
        source_id=source_id,
        cn_name=cn_name,
    )
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to save file")

    # Log edit operation
    bbk_id = x_bbk_id
    if svc.db.is_connected:
        try:
            await svc.db.execute(
                """
                INSERT INTO swe_user_item_operation_logs
                    (source_id, user_id, user_name, bbk_id, operation,
                     item_type, item_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    source_id,
                    x_user_id,
                    decode_user_name(x_user_name),
                    bbk_id,
                    "edit",
                    "skill",
                    skill_name,
                ),
            )
        except Exception as e:
            logger.warning("Failed to log edit operation: %s", e)

    # 更新数据库 swe_skills 表
    if cn_name and new_version:
        await svc.skill_registry.update_skill(
            user_id=x_user_id,
            skill_name=skill_name,
            source_id=source_id,
            cn_name=cn_name,
            version=new_version,
        )

    return OperationResponse(success=True)


@router.delete(
    "/market/skills/mine/{skill_name}",
    response_model=OperationResponse,
)
async def delete_my_skill(
    skill_name: str,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_user_name: Optional[str] = Header(default=None, alias="X-User-Name"),
    x_bbk_id: Optional[str] = Header(default=None, alias="X-Bbk-Id"),
    agent_id: str = "default",
):
    """删除技能."""
    source_id = require_source_id(x_source_id)
    if not x_user_id:
        raise HTTPException(
            status_code=400,
            detail="X-User-Id header is required",
        )

    log_params(
        logger,
        request.method,
        request.url.path,
        skill_name=skill_name,
        agent_id=agent_id,
    )

    svc = request.app.state.marketplace
    ok = await svc.delete_skill(x_user_id, skill_name, agent_id, source_id)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail="Skill not found or delete failed",
        )

    # Log delete operation
    bbk_id = x_bbk_id
    if svc.db.is_connected:
        try:
            await svc.db.execute(
                """
                INSERT INTO swe_user_item_operation_logs
                    (source_id, user_id, user_name, bbk_id, operation,
                     item_type, item_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    source_id,
                    x_user_id,
                    decode_user_name(x_user_name),
                    bbk_id,
                    "delete",
                    "skill",
                    skill_name,
                ),
            )
        except Exception as e:
            logger.warning("Failed to log delete operation: %s", e)

    return OperationResponse(success=True)


@router.post(
    "/market/skills/mine/{skill_name}/enable",
    response_model=OperationResponse,
)
async def enable_my_skill(
    skill_name: str,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_bbk_id: Optional[str] = Header(default=None, alias="X-Bbk-Id"),
    agent_id: str = "default",
):
    """启用技能（含安全扫描）."""
    source_id = require_source_id(x_source_id)
    if not x_user_id:
        raise HTTPException(
            status_code=400,
            detail="X-User-Id header is required",
        )

    log_params(
        logger,
        request.method,
        request.url.path,
        skill_name=skill_name,
        agent_id=agent_id,
    )

    svc = request.app.state.marketplace
    result = await svc.enable_skill(
        x_user_id,
        skill_name,
        agent_id,
        source_id,
        x_bbk_id or "",
    )
    if not result.get("success"):
        if result.get("reason") == "security_scan_failed":
            await _flush_skill_scan_history(request)
            raise HTTPException(
                status_code=422,
                detail=result,
            )
        raise HTTPException(
            status_code=404,
            detail=result.get("reason", "Skill not found"),
        )
    return OperationResponse(success=True)


@router.post(
    "/market/skills/mine/{skill_name}/disable",
    response_model=OperationResponse,
)
async def disable_my_skill(
    skill_name: str,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    agent_id: str = "default",
):
    """禁用技能."""
    source_id = require_source_id(x_source_id)
    if not x_user_id:
        raise HTTPException(
            status_code=400,
            detail="X-User-Id header is required",
        )

    log_params(
        logger,
        request.method,
        request.url.path,
        skill_name=skill_name,
        agent_id=agent_id,
    )

    svc = request.app.state.marketplace
    result = await svc.disable_skill(
        x_user_id,
        skill_name,
        agent_id,
        source_id,
    )
    if not result.get("success"):
        raise HTTPException(
            status_code=404,
            detail="Skill not found",
        )
    return OperationResponse(success=True)


@router.post(
    "/market/skills/mine/batch-delete",
    response_model=BatchOperationResponse,
)
async def batch_delete_my_skills(
    body: BatchOperationRequest,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    agent_id: str = "default",
):
    """批量删除技能."""
    source_id = require_source_id(x_source_id)
    if not x_user_id:
        raise HTTPException(
            status_code=400,
            detail="X-User-Id header is required",
        )

    log_params(
        logger,
        request.method,
        request.url.path,
        skills=body.skills,
        agent_id=agent_id,
    )

    svc = request.app.state.marketplace
    results = await svc.batch_delete_skills(
        x_user_id,
        body.skills,
        agent_id,
        source_id,
    )
    success_count = sum(1 for r in results.values() if r.get("success"))
    return BatchOperationResponse(
        results=results,
        success_count=success_count,
        failed_count=len(body.skills) - success_count,
    )


@router.post(
    "/market/skills/mine/batch-enable",
    response_model=BatchOperationResponse,
)
async def batch_enable_my_skills(
    body: BatchOperationRequest,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_bbk_id: Optional[str] = Header(default=None, alias="X-Bbk-Id"),
    agent_id: str = "default",
):
    """批量启用技能."""
    source_id = require_source_id(x_source_id)
    if not x_user_id:
        raise HTTPException(
            status_code=400,
            detail="X-User-Id header is required",
        )

    log_params(
        logger,
        request.method,
        request.url.path,
        skills=body.skills,
        agent_id=agent_id,
    )

    svc = request.app.state.marketplace
    results = await svc.batch_enable_skills(
        x_user_id,
        body.skills,
        agent_id,
        source_id,
        x_bbk_id or "",
    )
    if any(
        result.get("reason") == "security_scan_failed"
        for result in results.values()
    ):
        await _flush_skill_scan_history(request)
    success_count = sum(1 for r in results.values() if r.get("success"))
    return BatchOperationResponse(
        results=results,
        success_count=success_count,
        failed_count=len(body.skills) - success_count,
    )


@router.post(
    "/market/skills/mine/batch-disable",
    response_model=BatchOperationResponse,
)
async def batch_disable_my_skills(
    body: BatchOperationRequest,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    agent_id: str = "default",
):
    """批量禁用技能."""
    source_id = require_source_id(x_source_id)
    if not x_user_id:
        raise HTTPException(
            status_code=400,
            detail="X-User-Id header is required",
        )

    log_params(
        logger,
        request.method,
        request.url.path,
        skills=body.skills,
        agent_id=agent_id,
    )

    svc = request.app.state.marketplace
    results = await svc.batch_disable_skills(
        x_user_id,
        body.skills,
        agent_id,
        source_id,
    )
    success_count = sum(1 for r in results.values() if r.get("success"))
    return BatchOperationResponse(
        results=results,
        success_count=success_count,
        failed_count=len(body.skills) - success_count,
    )


# -----------------------------------------------------------
# 操作日志上报端点
# -----------------------------------------------------------


class OperationLogRequest(BaseModel):
    """操作日志上报请求体。"""

    operation: str = Field(..., description="操作类型: create/edit/delete")
    item_type: str = Field(default="skill", description="条目类型: skill/mcp")
    item_name: str = Field(..., description="条目名称")
    user_name: Optional[str] = Field(default=None, description="用户名称")
    bbk_id: Optional[str] = Field(default=None, description="机构ID")


@router.post("/market/skills/operation-log")
async def log_skill_operation(
    request: Request,
    body: OperationLogRequest,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """上报技能操作日志。

    用于 Agent 通过 skill_creator 创建技能后记录操作日志。
    采用失败忽略策略，写入失败不影响业务。
    """
    source_id = require_source_id(x_source_id)
    if not x_user_id:
        raise HTTPException(
            status_code=400,
            detail="X-User-Id header is required",
        )

    svc = request.app.state.marketplace
    user_name = decode_user_name(body.user_name) or x_user_id
    bbk_id = body.bbk_id

    if svc.db.is_connected:
        try:
            await svc.db.execute(
                """
                INSERT INTO swe_user_item_operation_logs
                    (source_id, user_id, user_name, bbk_id, operation,
                     item_type, item_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    source_id,
                    x_user_id,
                    user_name,
                    bbk_id,
                    body.operation,
                    body.item_type,
                    body.item_name,
                ),
            )
        except Exception as e:
            logger.warning("Failed to log operation: %s", e)

    return {"success": True}


class MigrateSkillJsonRequest(BaseModel):
    """迁移 skill.json 字段请求."""

    delete_skill_json: bool = Field(
        default=False,
        description="是否删除技能目录内的 skill.json 文件",
    )


class MigrateSkillJsonResponse(BaseModel):
    """迁移 skill.json 字段响应."""

    migrated: int = Field(description="成功迁移的技能数量")
    skipped: int = Field(
        description="跳过的技能数量（无 skill.json 或无额外字段）",
    )
    errors: list[str] = Field(default_factory=list, description="错误信息列表")


@router.post(
    "/market/skills/migrate-skill-json",
    response_model=MigrateSkillJsonResponse,
)
async def migrate_skill_json_to_manifest(
    request: Request,
    body: MigrateSkillJsonRequest,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    agent_id: str = "default",
):
    """迁移技能目录内 skill.json 字段到 workspace manifest.

    将以下字段从 skills/<技能名>/skill.json 合并到
    workspaces/<agent_id>/skill.json:
    - creator_id
    - creator_name
    - bbk_id
    - distributed_by
    - received_version
    - category_id

    Args:
        delete_skill_json: 是否删除技能目录内的 skill.json 文件（默认不删除）
    """
    source_id = require_source_id(x_source_id)
    if not x_user_id:
        raise HTTPException(
            status_code=400,
            detail="X-User-Id header is required",
        )

    svc = request.app.state.marketplace
    result = svc.migrate_skill_json_to_manifest(
        x_user_id,
        agent_id,
        source_id,
        delete_skill_json=body.delete_skill_json,
    )

    return MigrateSkillJsonResponse(
        migrated=result["migrated"],
        skipped=result["skipped"],
        errors=result["errors"],
    )
