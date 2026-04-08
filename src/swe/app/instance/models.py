# -*- coding: utf-8 -*-
"""Instance management data models."""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class InstanceStatus(str, Enum):
    """Instance status."""

    ACTIVE = "active"
    INACTIVE = "inactive"


class UserAllocationStatus(str, Enum):
    """User allocation status."""

    ACTIVE = "active"
    MIGRATED = "migrated"


class ActionType(str, Enum):
    """Action types for logging."""

    # Instance actions
    CREATE_INSTANCE = "create_instance"
    UPDATE_INSTANCE = "update_instance"
    DELETE_INSTANCE = "delete_instance"

    # Allocation actions
    ALLOCATE = "allocate"
    MIGRATE = "migrate"
    DELETE_ALLOCATION = "delete_allocation"


class TargetType(str, Enum):
    """Target types for logging."""

    SOURCE = "source"
    INSTANCE = "instance"
    USER = "user"


# Base models


class Source(BaseModel):
    """User source model."""

    source_id: str
    source_name: str
    created_at: Optional[datetime] = None


class SourceWithStats(Source):
    """Source with statistics."""

    total_instances: int = 0
    total_users: int = 0
    active_instances: int = 0


class Instance(BaseModel):
    """Instance model."""

    model_config = ConfigDict(use_enum_values=True)

    instance_id: str
    source_id: str
    bbk_id: Optional[str] = None
    instance_name: str
    instance_url: str
    max_users: int = 100
    status: InstanceStatus = InstanceStatus.ACTIVE
    created_at: Optional[datetime] = None


class InstanceWithUsage(Instance):
    """Instance with usage statistics."""

    current_users: int = 0
    usage_percent: float = 0.0
    warning_level: str = "normal"  # normal, warning, critical

    # Related names
    source_name: Optional[str] = None
    bbk_name: Optional[str] = None


class UserAllocation(BaseModel):
    """User allocation record."""

    model_config = ConfigDict(use_enum_values=True)

    id: Optional[int] = None
    user_id: str
    source_id: str
    instance_id: str
    allocated_at: Optional[datetime] = None
    status: UserAllocationStatus = UserAllocationStatus.ACTIVE

    # Related names
    source_name: Optional[str] = None
    instance_name: Optional[str] = None
    instance_url: Optional[str] = None


class OperationLog(BaseModel):
    """Operation log record."""

    id: Optional[int] = None
    action: str
    target_type: str
    target_id: str
    old_value: Optional[dict[str, Any]] = None
    new_value: Optional[dict[str, Any]] = None
    operator: Optional[str] = None
    created_at: Optional[datetime] = None


# Request models


class CreateInstanceRequest(BaseModel):
    """Create instance request."""

    instance_id: str = Field(..., min_length=1, max_length=64)
    source_id: str = Field(..., min_length=1, max_length=64)
    bbk_id: Optional[str] = Field(None, max_length=64)
    instance_name: str = Field(..., min_length=1, max_length=128)
    instance_url: str = Field(..., min_length=1, max_length=512)
    max_users: int = Field(default=100, ge=1, le=10000)


class UpdateInstanceRequest(BaseModel):
    """Update instance request."""

    instance_name: Optional[str] = Field(None, min_length=1, max_length=128)
    instance_url: Optional[str] = Field(None, min_length=1, max_length=512)
    max_users: Optional[int] = Field(None, ge=1, le=10000)
    status: Optional[InstanceStatus] = None


class AllocateUserRequest(BaseModel):
    """User allocation request."""

    user_id: str = Field(..., min_length=1, max_length=128)
    source_id: str = Field(..., min_length=1, max_length=64)
    instance_id: Optional[str] = Field(
        None,
        max_length=64,
    )  # Optional, auto-allocate if not provided


class MigrateUserRequest(BaseModel):
    """User migration request."""

    user_id: str = Field(..., min_length=1, max_length=128)
    source_id: str = Field(..., min_length=1, max_length=64)
    target_instance_id: str = Field(..., min_length=1, max_length=64)


class DeleteAllocationRequest(BaseModel):
    """Delete allocation request."""

    user_id: str = Field(..., min_length=1, max_length=128)
    source_id: str = Field(..., min_length=1, max_length=64)


# Response models


class AllocateUserResponse(BaseModel):
    """User allocation response."""

    success: bool
    user_id: str
    source_id: str
    instance_id: Optional[str] = None
    instance_name: Optional[str] = None
    instance_url: Optional[str] = None
    message: Optional[str] = None


class UserInstanceUrlResponse(BaseModel):
    """User instance URL query response."""

    success: bool
    user_id: str
    source_id: Optional[str] = None
    source_name: Optional[str] = None
    instance_id: Optional[str] = None
    instance_name: Optional[str] = None
    instance_url: Optional[str] = None
    allocated_at: Optional[datetime] = None
    message: Optional[str] = None


class SourceListResponse(BaseModel):
    """Source list response."""

    sources: list[SourceWithStats]
    total: int


class InstanceListResponse(BaseModel):
    """Instance list response."""

    instances: list[InstanceWithUsage]
    total: int


class AllocationListResponse(BaseModel):
    """Allocation list response."""

    allocations: list[UserAllocation]
    total: int


class LogListResponse(BaseModel):
    """Log list response."""

    logs: list[OperationLog]
    total: int


class OverviewStats(BaseModel):
    """Overview statistics."""

    total_instances: int = 0
    total_users: int = 0
    active_instances: int = 0
    warning_instances: int = 0
    critical_instances: int = 0
