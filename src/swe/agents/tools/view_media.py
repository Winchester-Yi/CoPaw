# -*- coding: utf-8 -*-
"""Load image or video files into the LLM context for analysis."""

import mimetypes
import unicodedata
from pathlib import Path
from typing import Optional

from agentscope.message import ImageBlock, TextBlock, VideoBlock
from agentscope.tool import ToolResponse

from ...security.tenant_path_boundary import (
    resolve_tenant_path,
    TenantPathBoundaryError,
    make_permission_denied_response,
)

_IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".tiff",
    ".tif",
}

_VIDEO_EXTENSIONS = {
    ".mp4",
    ".webm",
    ".mpeg",
    ".mov",
    ".avi",
    ".mkv",
}


def _validate_media_path(
    file_path: str,
    allowed_extensions: set[str],
    mime_prefix: str,
) -> tuple[Optional[Path], Optional[ToolResponse]]:
    """Validate a media file path using tenant boundary.

    Returns ``(resolved_path, None)`` on success or
    ``(_, error_response)`` on failure.
    """
    # Normalize the path
    file_path = unicodedata.normalize("NFC", file_path)

    # Validate path against tenant boundary
    try:
        resolved = resolve_tenant_path(file_path)
    except TenantPathBoundaryError:
        return None, ToolResponse(
            content=[TextBlock(**make_permission_denied_response("View media"))],
        )

    if not resolved.exists() or not resolved.is_file():
        return None, ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=f"Error: {file_path} does not exist "
                    "or is not a file.",
                ),
            ],
        )

    ext = resolved.suffix.lower()
    mime, _ = mimetypes.guess_type(str(resolved))
    if ext not in allowed_extensions and (
        not mime or not mime.startswith(f"{mime_prefix}/")
    ):
        return None, ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=f"Error: {resolved.name} is not a "
                    f"supported {mime_prefix} format.",
                ),
            ],
        )

    return resolved, None


async def view_image(image_path: str) -> ToolResponse:
    """Load an image file into the LLM context so the model can see it.

    Use this after desktop_screenshot, browser_use, or any tool that
    produces an image file path.

    Args:
        image_path (`str`):
            Path to the image file to view.

    Returns:
        `ToolResponse`:
            An ImageBlock the model can inspect, or an error message.
    """
    resolved, err = _validate_media_path(
        image_path,
        _IMAGE_EXTENSIONS,
        "image",
    )
    if err is not None or resolved is None:
        return err if err is not None else ToolResponse(
            content=[TextBlock(type="text", text="Error: Failed to validate media path.")],
        )

    return ToolResponse(
        content=[
            ImageBlock(
                type="image",
                source={"type": "url", "url": str(resolved)},
            ),
            TextBlock(
                type="text",
                text=f"Image loaded: {resolved.name}",
            ),
        ],
    )


async def view_video(video_path: str) -> ToolResponse:
    """Load a video file into the LLM context so the model can see it.

    Use this when the user asks about a video file or when another
    tool produces a video file path.

    Args:
        video_path (`str`):
            Path to the video file to view.

    Returns:
        `ToolResponse`:
            A VideoBlock the model can inspect, or an error message.
    """
    resolved, err = _validate_media_path(
        video_path,
        _VIDEO_EXTENSIONS,
        "video",
    )
    if err is not None or resolved is None:
        return err if err is not None else ToolResponse(
            content=[TextBlock(type="text", text="Error: Failed to validate media path.")],
        )

    return ToolResponse(
        content=[
            VideoBlock(
                type="video",
                source={"type": "url", "url": str(resolved)},
            ),
            TextBlock(
                type="text",
                text=f"Video loaded: {resolved.name}",
            ),
        ],
    )
