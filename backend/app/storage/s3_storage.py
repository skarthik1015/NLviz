"""S3 storage helper.

Thin wrapper around boto3 that:
- Accepts an injected client for testability (pass a mock).
- Provides typed methods for the operations the app actually uses.
- Exposes a static URL parser so callers don't parse s3:// strings themselves.
"""
from __future__ import annotations

import io
import logging
from typing import IO

logger = logging.getLogger(__name__)


class S3Storage:
    """Wraps a single S3 bucket for a specific purpose (uploads or schemas).

    The underlying boto3 client is created lazily on first use so that the
    class can be instantiated without AWS credentials available (e.g. unit tests
    that inject a mock client).
    """

    def __init__(self, bucket: str, region: str, client=None) -> None:
        """
        Args:
            bucket: S3 bucket name.
            region: AWS region string (used when creating the default client).
            client: Optional pre-built boto3 S3 client (for testing).
        """
        self.bucket = bucket
        self.region = region
        self._client = client

    def _get_client(self):
        if self._client is None:
            import boto3  # lazy import — not required in local dev mode
            self._client = boto3.client("s3", region_name=self.region)
        return self._client

    # ── Write ─────────────────────────────────────────────────────────

    def upload_fileobj(self, key: str, fileobj: IO[bytes]) -> str:
        """Upload a file-like object and return its s3:// URL."""
        logger.debug("Uploading to s3://%s/%s", self.bucket, key)
        self._get_client().upload_fileobj(fileobj, self.bucket, key)
        return f"s3://{self.bucket}/{key}"

    def upload_bytes(self, key: str, data: bytes) -> str:
        """Upload raw bytes and return the s3:// URL."""
        logger.debug("Uploading %d bytes to s3://%s/%s", len(data), self.bucket, key)
        self._get_client().put_object(Body=data, Bucket=self.bucket, Key=key)
        return f"s3://{self.bucket}/{key}"

    # ── Read ──────────────────────────────────────────────────────────

    def download_bytes(self, key: str) -> bytes:
        """Download an object and return its contents as bytes."""
        logger.debug("Downloading s3://%s/%s", self.bucket, key)
        response = self._get_client().get_object(Bucket=self.bucket, Key=key)
        return response["Body"].read()

    def download_fileobj(self, key: str) -> io.BytesIO:
        """Download an object and return a seekable BytesIO buffer."""
        data = self.download_bytes(key)
        buf = io.BytesIO(data)
        buf.seek(0)
        return buf

    # ── Delete ────────────────────────────────────────────────────────

    def delete(self, key: str) -> None:
        """Delete an object from the bucket."""
        logger.debug("Deleting s3://%s/%s", self.bucket, key)
        self._get_client().delete_object(Bucket=self.bucket, Key=key)

    # ── Utilities ─────────────────────────────────────────────────────

    @staticmethod
    def parse_s3_url(url: str) -> tuple[str, str]:
        """Parse an s3:// URL into (bucket, key).

        Raises ValueError for malformed URLs.
        """
        if not url.startswith("s3://"):
            raise ValueError(f"Not an S3 URL: {url!r}")
        remainder = url[5:]
        if "/" not in remainder:
            raise ValueError(f"S3 URL has no key component: {url!r}")
        bucket, key = remainder.split("/", 1)
        if not bucket or not key:
            raise ValueError(f"S3 URL has empty bucket or key: {url!r}")
        return bucket, key

    def key_from_url(self, url: str) -> str:
        """Extract just the key from an s3:// URL that belongs to this bucket."""
        bucket, key = self.parse_s3_url(url)
        if bucket != self.bucket:
            raise ValueError(f"URL bucket {bucket!r} does not match this storage bucket {self.bucket!r}")
        return key
