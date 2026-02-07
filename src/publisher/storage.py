"""
Cloudflare R2 storage client for Sticker Trendz.

Uses boto3 (S3-compatible API) to upload sticker images, database
backups, and manage objects in the R2 bucket.
"""

from __future__ import annotations

import logging
from io import BytesIO
from typing import List, Optional

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from src.config import load_config

logger = logging.getLogger(__name__)


class StorageError(Exception):
    """Raised on R2 storage operation failures."""


class R2StorageClient:
    """
    Cloudflare R2 storage client using the S3-compatible API.

    Handles uploading sticker images (original, print-ready, thumbnail),
    database backups, and listing/deleting objects.
    """

    def __init__(
        self,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        bucket: Optional[str] = None,
        endpoint: Optional[str] = None,
        public_url: Optional[str] = None,
        client: Optional[object] = None,
    ) -> None:
        if client:
            self._client = client
            self._bucket = bucket or ""
            self._public_url = public_url or ""
            return

        cfg = load_config(require_all=False)
        self._access_key = access_key or cfg.r2.access_key
        self._secret_key = secret_key or cfg.r2.secret_key
        self._bucket = bucket or cfg.r2.bucket
        self._endpoint = endpoint or cfg.r2.endpoint
        self._public_url = public_url or cfg.r2.public_url

        try:
            self._client = boto3.client(
                "s3",
                endpoint_url=self._endpoint,
                aws_access_key_id=self._access_key,
                aws_secret_access_key=self._secret_key,
                config=BotoConfig(
                    signature_version="s3v4",
                    retries={"max_attempts": 3, "mode": "adaptive"},
                ),
            )
            logger.info("R2 storage client initialized for bucket '%s'", self._bucket)
        except Exception as exc:
            raise StorageError(
                f"Failed to initialize R2 storage client: {exc}"
            ) from exc

    def upload_image(
        self,
        key: str,
        data: bytes,
        content_type: str = "image/png",
        cache_control: str = "public, max-age=31536000, immutable",
    ) -> str:
        """
        Upload an image to R2.

        Args:
            key: Object key (path in the bucket).
            data: Raw image bytes.
            content_type: MIME type (default: image/png).
            cache_control: Cache-Control header for CDN.

        Returns:
            Public URL of the uploaded object.
        """
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                CacheControl=cache_control,
            )
            url = f"{self._public_url}/{key}" if self._public_url else key
            logger.info("Uploaded image to R2: %s (%d bytes)", key, len(data))
            return url
        except ClientError as exc:
            logger.error("R2 upload failed for key '%s': %s", key, exc)
            raise StorageError(f"R2 upload failed for '{key}': {exc}") from exc

    def upload_backup(
        self,
        key: str,
        data: bytes,
    ) -> str:
        """
        Upload a gzipped database backup to R2.

        Args:
            key: Object key (e.g. backups/db/sticker-trendz-2024-01-15.sql.gz).
            data: Gzipped SQL dump bytes.

        Returns:
            The object key.
        """
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType="application/gzip",
            )
            logger.info("Uploaded backup to R2: %s (%d bytes)", key, len(data))
            return key
        except ClientError as exc:
            logger.error("R2 backup upload failed for key '%s': %s", key, exc)
            raise StorageError(f"R2 backup upload failed for '{key}': {exc}") from exc

    def delete_object(self, key: str) -> None:
        """Delete an object from R2 by key."""
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
            logger.info("Deleted object from R2: %s", key)
        except ClientError as exc:
            logger.error("R2 delete failed for key '%s': %s", key, exc)
            raise StorageError(f"R2 delete failed for '{key}': {exc}") from exc

    def list_objects(self, prefix: str, max_keys: int = 1000) -> List[dict]:
        """
        List objects under a prefix.

        Returns:
            List of dicts with 'Key', 'Size', 'LastModified'.
        """
        try:
            response = self._client.list_objects_v2(
                Bucket=self._bucket,
                Prefix=prefix,
                MaxKeys=max_keys,
            )
            contents = response.get("Contents", [])
            logger.debug("Listed %d objects under prefix '%s'", len(contents), prefix)
            return contents
        except ClientError as exc:
            logger.error("R2 list failed for prefix '%s': %s", prefix, exc)
            raise StorageError(f"R2 list failed for '{prefix}': {exc}") from exc

    def get_object(self, key: str) -> bytes:
        """Download an object from R2 and return its bytes."""
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            data = response["Body"].read()
            logger.debug("Downloaded object from R2: %s (%d bytes)", key, len(data))
            return data
        except ClientError as exc:
            logger.error("R2 get failed for key '%s': %s", key, exc)
            raise StorageError(f"R2 get failed for '{key}': {exc}") from exc
