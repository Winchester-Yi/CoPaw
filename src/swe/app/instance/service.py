# -*- coding: utf-8 -*-
"""Instance management service layer."""

import logging
from typing import Optional

from .models import (
    ActionType,
    AllocateUserResponse,
    CreateInstanceRequest,
    DeleteAllocationRequest,
    Instance,
    InstanceStatus,
    MigrateUserRequest,
    TargetType,
    UpdateInstanceRequest,
    UserInstanceUrlResponse,
)
from .store import InstanceStore

logger = logging.getLogger(__name__)


class InstanceService:
    """Service for instance management operations."""

    def __init__(self, store: InstanceStore):
        """Initialize service.

        Args:
            store: Instance store instance
        """
        self.store = store

    # ==================== Instance operations ====================

    async def create_instance(
        self,
        request: CreateInstanceRequest,
        operator: Optional[str] = None,
    ) -> Instance:
        """Create a new instance."""
        # Check if instance already exists
        existing = await self.store.get_instance(request.instance_id)
        if existing:
            raise ValueError(f"实例 {request.instance_id} 已存在")

        instance = await self.store.create_instance(
            instance_id=request.instance_id,
            source_id=request.source_id,
            instance_name=request.instance_name,
            instance_url=request.instance_url,
            max_users=request.max_users,
            bbk_id=request.bbk_id,
        )

        # Log
        await self.store.create_log(
            action=ActionType.CREATE_INSTANCE.value,
            target_type=TargetType.INSTANCE.value,
            target_id=request.instance_id,
            new_value={
                "instance_id": request.instance_id,
                "source_id": request.source_id,
                "bbk_id": request.bbk_id,
                "instance_name": request.instance_name,
                "instance_url": request.instance_url,
                "max_users": request.max_users,
            },
            operator=operator,
        )

        return instance

    async def update_instance(
        self,
        instance_id: str,
        request: UpdateInstanceRequest,
        operator: Optional[str] = None,
    ) -> Instance:
        """Update instance."""
        existing = await self.store.get_instance(instance_id)
        if not existing:
            raise ValueError(f"实例 {instance_id} 不存在")

        old_values = {}
        if request.instance_name is not None:
            old_values["instance_name"] = existing.instance_name
        if request.instance_url is not None:
            old_values["instance_url"] = existing.instance_url
        if request.max_users is not None:
            old_values["max_users"] = existing.max_users
        if request.status is not None:
            old_values["status"] = existing.status.value

        await self.store.update_instance(
            instance_id=instance_id,
            instance_name=request.instance_name,
            instance_url=request.instance_url,
            max_users=request.max_users,
            status=request.status,
        )

        # Log
        new_values = {
            k: v
            for k, v in {
                "instance_name": request.instance_name,
                "instance_url": request.instance_url,
                "max_users": request.max_users,
                "status": request.status.value if request.status else None,
            }.items()
            if v is not None
        }
        await self.store.create_log(
            action=ActionType.UPDATE_INSTANCE.value,
            target_type=TargetType.INSTANCE.value,
            target_id=instance_id,
            old_value=old_values,
            new_value=new_values,
            operator=operator,
        )

        updated = await self.store.get_instance(instance_id)
        if updated is None:
            raise ValueError(f"实例 {instance_id} 更新后查询失败")
        return updated

    async def delete_instance(
        self,
        instance_id: str,
        operator: Optional[str] = None,
    ) -> bool:
        """Delete instance."""
        existing = await self.store.get_instance(instance_id)
        if not existing:
            raise ValueError(f"实例 {instance_id} 不存在")

        success = await self.store.delete_instance(instance_id)
        if not success:
            raise ValueError("该实例下存在用户分配，无法删除")

        # Log
        await self.store.create_log(
            action=ActionType.DELETE_INSTANCE.value,
            target_type=TargetType.INSTANCE.value,
            target_id=instance_id,
            old_value={
                "instance_id": instance_id,
                "instance_name": existing.instance_name,
            },
            operator=operator,
        )

        return True

    # ==================== User allocation operations ====================

    async def allocate_user(
        self,
        request: dict,
        operator: Optional[str] = None,
    ) -> AllocateUserResponse:
        """Allocate user to an instance.

        If instance_id is provided, use it directly.
        Otherwise, auto-select the instance with lowest load.
        """
        user_id = request.get("user_id")
        source_id = request.get("source_id")
        instance_id = request.get("instance_id")

        # Validate required fields
        if not user_id or not source_id:
            raise ValueError("user_id 和 source_id 为必填字段")

        # Check if already allocated
        existing = await self.store.get_allocation(user_id, source_id)
        if existing:
            return AllocateUserResponse(
                success=False,
                user_id=user_id,
                source_id=source_id,
                instance_id=existing.instance_id,
                instance_name=existing.instance_name,
                instance_url=existing.instance_url,
                message=f"用户已分配到实例 {existing.instance_id}",
            )

        if instance_id:
            # Manual allocation
            instance = await self.store.get_instance_with_usage(instance_id)
            if not instance:
                raise ValueError(f"实例 {instance_id} 不存在")

            # Validate instance matches source
            if instance.source_id != source_id:
                raise ValueError("实例不属于该来源")

            # Check threshold
            if instance.current_users >= instance.max_users:
                return AllocateUserResponse(
                    success=False,
                    user_id=user_id,
                    source_id=source_id,
                    message="实例已达阈值，请选择其他实例或扩容",
                )
        else:
            # Auto allocation - load balancing
            instances = await self.store.get_available_instances(source_id)
            if not instances:
                return AllocateUserResponse(
                    success=False,
                    user_id=user_id,
                    source_id=source_id,
                    message="该来源无可用实例，请先添加实例",
                )

            # Filter instances below threshold
            available = [i for i in instances if i.current_users < i.max_users]
            if not available:
                return AllocateUserResponse(
                    success=False,
                    user_id=user_id,
                    source_id=source_id,
                    message="所有实例已达阈值，请扩容",
                )

            # Select instance with most remaining capacity (best fit)
            selected_instance = max(
                available,
                key=lambda x: x.max_users - x.current_users,
            )
            instance_id = selected_instance.instance_id

        # Create allocation
        await self.store.create_allocation(
            user_id=user_id,
            source_id=source_id,
            instance_id=instance_id,
        )

        # Log
        await self.store.create_log(
            action=ActionType.ALLOCATE.value,
            target_type=TargetType.USER.value,
            target_id=user_id,
            new_value={
                "source_id": source_id,
                "instance_id": instance_id,
            },
            operator=operator,
        )

        # Get updated instance info
        updated_instance = await self.store.get_instance_with_usage(
            instance_id,
        )

        return AllocateUserResponse(
            success=True,
            user_id=user_id,
            source_id=source_id,
            instance_id=instance_id,
            instance_name=updated_instance.instance_name
            if updated_instance
            else None,
            instance_url=updated_instance.instance_url
            if updated_instance
            else None,
            message="分配成功",
        )

    async def migrate_user(
        self,
        request: MigrateUserRequest,
        operator: Optional[str] = None,
    ) -> AllocateUserResponse:
        """Migrate user to another instance."""
        # Get current allocation
        existing = await self.store.get_allocation(
            request.user_id,
            request.source_id,
        )
        if not existing:
            raise ValueError("用户未分配实例")

        old_instance_id = existing.instance_id

        # Validate target instance
        target_instance = await self.store.get_instance_with_usage(
            request.target_instance_id,
        )
        if not target_instance:
            raise ValueError(f"目标实例 {request.target_instance_id} 不存在")

        if target_instance.status != InstanceStatus.ACTIVE:
            raise ValueError("目标实例不可用")

        if target_instance.source_id != request.source_id:
            raise ValueError("目标实例不属于该来源")

        # Check threshold
        if target_instance.current_users >= target_instance.max_users:
            return AllocateUserResponse(
                success=False,
                user_id=request.user_id,
                source_id=request.source_id,
                instance_id=request.target_instance_id,
                message="目标实例已达阈值",
            )

        # Update allocation
        await self.store.update_allocation_instance(
            user_id=request.user_id,
            source_id=request.source_id,
            new_instance_id=request.target_instance_id,
        )

        # Log
        await self.store.create_log(
            action=ActionType.MIGRATE.value,
            target_type=TargetType.USER.value,
            target_id=request.user_id,
            old_value={"instance_id": old_instance_id},
            new_value={"instance_id": request.target_instance_id},
            operator=operator,
        )

        return AllocateUserResponse(
            success=True,
            user_id=request.user_id,
            source_id=request.source_id,
            instance_id=request.target_instance_id,
            instance_name=target_instance.instance_name,
            instance_url=target_instance.instance_url,
            message="迁移成功",
        )

    async def delete_allocation(
        self,
        request: DeleteAllocationRequest,
        operator: Optional[str] = None,
    ) -> bool:
        """Delete user allocation."""
        existing = await self.store.get_allocation(
            request.user_id,
            request.source_id,
        )
        if not existing:
            raise ValueError("用户分配记录不存在")

        success = await self.store.delete_allocation(
            user_id=request.user_id,
            source_id=request.source_id,
        )

        if success:
            await self.store.create_log(
                action=ActionType.DELETE_ALLOCATION.value,
                target_type=TargetType.USER.value,
                target_id=request.user_id,
                old_value={
                    "source_id": request.source_id,
                    "instance_id": existing.instance_id,
                },
                operator=operator,
            )

        return success

    async def get_user_instance_url(
        self,
        user_id: str,
        source_id: str,
    ) -> UserInstanceUrlResponse:
        """Get user's instance URL."""
        allocation = await self.store.get_allocation(user_id, source_id)

        if not allocation:
            return UserInstanceUrlResponse(
                success=False,
                user_id=user_id,
                source_id=source_id,
                message="用户未分配实例",
            )

        source = await self.store.get_source(allocation.source_id)

        return UserInstanceUrlResponse(
            success=True,
            user_id=user_id,
            source_id=allocation.source_id,
            source_name=source.source_name if source else None,
            instance_id=allocation.instance_id,
            instance_name=allocation.instance_name,
            instance_url=allocation.instance_url,
            allocated_at=allocation.allocated_at,
        )
