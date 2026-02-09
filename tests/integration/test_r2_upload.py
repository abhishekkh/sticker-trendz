"""
Integration tests for R2StorageClient.

Requires CLOUDFLARE_R2_ACCESS_KEY, CLOUDFLARE_R2_SECRET_KEY,
CLOUDFLARE_R2_BUCKET, and CLOUDFLARE_R2_ENDPOINT environment variables.

All test objects are stored under 'test-integration/{uuid}/' and cleaned up.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import skip_if_no_r2, cleanup_r2_objects

pytestmark = [pytest.mark.integration, skip_if_no_r2]

# 1x1 red PNG (67 bytes)
TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
    b"\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
    b"\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
def r2_test_prefix():
    return f"test-integration/{uuid.uuid4().hex[:8]}"


@pytest.fixture
def storage():
    from src.publisher.storage import R2StorageClient
    return R2StorageClient()


@pytest.fixture(autouse=True)
def cleanup(storage, r2_test_prefix):
    """Cleanup test objects after each test."""
    yield
    cleanup_r2_objects(storage, r2_test_prefix)


class TestR2StorageIntegration:
    """Live R2 CRUD integration tests."""

    def test_upload_image(self, storage, r2_test_prefix):
        """upload_image stores bytes and returns a URL/key."""
        key = f"{r2_test_prefix}/test-upload.png"
        result = storage.upload_image(key, TINY_PNG)
        assert key in result or result == key

    def test_get_object_returns_exact_bytes(self, storage, r2_test_prefix):
        """get_object returns the exact bytes that were uploaded."""
        key = f"{r2_test_prefix}/test-get.png"
        storage.upload_image(key, TINY_PNG)
        downloaded = storage.get_object(key)
        assert downloaded == TINY_PNG

    def test_list_objects_finds_uploaded(self, storage, r2_test_prefix):
        """list_objects includes the uploaded object."""
        key = f"{r2_test_prefix}/test-list.png"
        storage.upload_image(key, TINY_PNG)
        objects = storage.list_objects(r2_test_prefix)
        keys = [obj["Key"] for obj in objects]
        assert key in keys

    def test_delete_object_removes_it(self, storage, r2_test_prefix):
        """delete_object removes the object from R2."""
        key = f"{r2_test_prefix}/test-delete.png"
        storage.upload_image(key, TINY_PNG)
        storage.delete_object(key)
        objects = storage.list_objects(r2_test_prefix)
        keys = [obj["Key"] for obj in objects]
        assert key not in keys

    def test_public_url_format(self, storage, r2_test_prefix):
        """Public URL contains the key path."""
        key = f"{r2_test_prefix}/test-url.png"
        url = storage.upload_image(key, TINY_PNG)
        assert r2_test_prefix in url
        assert "test-url.png" in url
