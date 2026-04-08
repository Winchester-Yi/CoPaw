# -*- coding: utf-8 -*-
"""AWS S3 client for backup operations."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from .config import BackupEnvironmentConfig


class S3BackupClient:
    """S3 client wrapper for backup operations.

    Storage path structure: {prefix}/{instance_id}/{YYYY-MM-DD}/{HH}/{tenant_id}.zip
    Example: swe_backup/instance-01/2026-03-25/14/12345678.zip
    """

    def __init__(self, config: BackupEnvironmentConfig):
        self.config = config
        self._s3 = boto3.client(
            "s3",
            aws_access_key_id=config.aws_access_key_id,
            aws_secret_access_key=config.aws_secret_access_key,
            region_name=config.s3_region,
            endpoint_url=config.endpoint_url,
        )
        self.bucket = config.s3_bucket
        self.prefix = config.s3_prefix

    def upload(
        self,
        local_path: Path,
        instance_id: str,
        date: str,
        hour: int,
        tenant_id: str,
    ) -> str:
        """Upload a file to S3.

        Args:
            local_path: Local file path to upload
            instance_id: Instance identifier
            date: Date folder (YYYY-MM-DD)
            hour: Hour of day (0-23)
            tenant_id: Tenant identifier

        Returns:
            Full S3 key
        """
        s3_key = f"{self.prefix}/{instance_id}/{date}/{str(hour).zfill(2)}/{tenant_id}.zip"
        self._s3.upload_file(str(local_path), self.bucket, s3_key)
        return s3_key

    def download(self, s3_key: str, local_path: Path) -> None:
        """Download a file from S3.

        Args:
            s3_key: Full S3 key (including prefix)
            local_path: Local destination path
        """
        local_path.parent.mkdir(parents=True, exist_ok=True)
        self._s3.download_file(self.bucket, s3_key, str(local_path))

    def list_backups(
        self,
        instance_id: Optional[str] = None,
        date: Optional[str] = None,
        hour: Optional[int] = None,
        tenant_id: Optional[str] = None,
    ) -> dict:
        """List available backups in S3.

        Args:
            instance_id: Filter by instance ID
            date: Filter by date (YYYY-MM-DD)
            hour: Filter by hour (0-23)
            tenant_id: Filter by tenant ID

        Returns:
            Dict with instances, dates, hours lists and backups grouped by instance/date/hour
            Structure:
            {
                "instances": ["instance-01", "instance-02"],
                "dates": ["2026-03-25", "2026-03-24"],
                "hours": [14, 13, 12],
                "backups": {
                    "instance-01": {
                        "2026-03-25": {
                            "14": {
                                "tenant123": {
                                    "s3_key": "...",
                                    "size": 1234,
                                    "last_modified": "...",
                                },
                            }
                        }
                    }
                }
            }
        """
        # Build prefix for listing
        prefix = f"{self.prefix}/"
        prefix = self.build_prefix_for_listing(date, hour, instance_id, prefix)

        backups = {}
        instances_set = set()
        dates_set = set()
        hours_set = set()
        continuation_token = None

        while True:
            params = {"Bucket": self.bucket, "Prefix": prefix}
            if continuation_token:
                params["ContinuationToken"] = continuation_token

            response = self._s3.list_objects_v2(**params)

            if "Contents" in response:
                for obj in response["Contents"]:
                    key = obj["Key"]
                    # Parse key: {prefix}/{instance_id}/{date}/{hour}/{tenant_id}.zip
                    parts = key.replace(f"{self.prefix}/", "").split("/")
                    if len(parts) != 4 or not parts[3].endswith(".zip"):
                        continue

                    backup_instance = parts[0]
                    backup_date = parts[1]
                    try:
                        backup_hour = int(parts[2])
                    except ValueError:
                        continue
                    backup_tenant_id = parts[3].replace(".zip", "")

                    # Apply filters
                    if self.apply_filters(
                        instance_id,
                        backup_instance,
                        date,
                        backup_date,
                        hour,
                        backup_hour,
                        tenant_id,
                        backup_tenant_id,
                    ):
                        continue

                    # Track unique values
                    instances_set.add(backup_instance)
                    dates_set.add(backup_date)
                    hours_set.add(backup_hour)

                    self.build_nested_structure(
                        backup_date,
                        backup_hour,
                        backup_instance,
                        backups,
                    )

                    backups[backup_instance][backup_date][backup_hour][
                        backup_tenant_id
                    ] = {
                        "s3_key": key,
                        "size": obj["Size"],
                        "last_modified": obj["LastModified"].isoformat(),
                    }

            if not response.get("IsTruncated"):
                break
            continuation_token = response.get("NextContinuationToken")

        # Sort results
        instances = sorted(instances_set, reverse=True)
        dates = sorted(dates_set, reverse=True)
        hours = sorted(hours_set, reverse=True)

        return {
            "instances": instances,
            "dates": dates,
            "hours": hours,
            "backups": backups,
        }

    def apply_filters(
        self,
        instance_id,
        backup_instance,
        date,
        backup_date,
        hour,
        backup_hour,
        tenant_id,
        backup_tenant_id,
    ):
        # Check each filter condition
        if instance_id and backup_instance != instance_id:
            return True
        if date and backup_date != date:
            return True
        if hour is not None and backup_hour != hour:
            return True
        if tenant_id and backup_tenant_id != tenant_id:
            return True
        return False

    def build_prefix_for_listing(self, date, hour, instance_id, prefix):
        if instance_id:
            prefix = f"{self.prefix}/{instance_id}/"
            if date:
                prefix = f"{self.prefix}/{instance_id}/{date}/"
                if hour is not None:
                    prefix = f"{self.prefix}/{instance_id}/{date}/{str(hour).zfill(2)}/"
        return prefix

    def build_nested_structure(
        self,
        backup_date,
        backup_hour,
        backup_instance,
        backups,
    ):
        # Build nested structure
        if backup_instance not in backups:
            backups[backup_instance] = {}
        if backup_date not in backups[backup_instance]:
            backups[backup_instance][backup_date] = {}
        if backup_hour not in backups[backup_instance][backup_date]:
            backups[backup_instance][backup_date][backup_hour] = {}

    def get_backup_key(
        self,
        instance_id: str,
        date: str,
        hour: int,
        tenant_id: str,
    ) -> str:
        """Get full S3 key for a backup.

        Args:
            instance_id: Instance identifier
            date: Date folder (YYYY-MM-DD)
            hour: Hour of day (0-23)
            tenant_id: Tenant identifier

        Returns:
            Full S3 key including prefix
        """
        return f"{self.prefix}/{instance_id}/{date}/{str(hour).zfill(2)}/{tenant_id}.zip"

    def backup_exists(
        self,
        instance_id: str,
        date: str,
        hour: int,
        tenant_id: str,
    ) -> bool:
        """Check if a backup exists in S3.

        Args:
            instance_id: Instance identifier
            date: Date folder (YYYY-MM-DD)
            hour: Hour of day (0-23)
            tenant_id: Tenant identifier

        Returns:
            True if backup exists, False otherwise
        """
        key = self.get_backup_key(instance_id, date, hour, tenant_id)
        try:
            self._s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False
            raise
