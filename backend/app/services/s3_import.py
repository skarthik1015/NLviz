"""Import one CSV/Parquet object from the user's S3 bucket.

The object is fetched by NLviz with the USER's credentials, or with an unsigned request for a
public bucket, and never with NLviz's own AWS identity: Jammi would open an s3:// URL with the
server's credentials (issue J25), and so would a default boto3 client. The file is then served
exactly like an upload (copied into managed storage, one Jammi source).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_S3_URL = re.compile(r"s3://(?P<bucket>[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9])/(?P<key>.+)")


class S3ImportError(ValueError):
    """Safe to show to the user."""


@dataclass(frozen=True)
class S3ImportRequest:
    s3_url: str
    region: str = "us-east-1"
    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None
    endpoint_url: str | None = None  # S3-compatible stores; only when the deployment allows it

    def bucket_key(self) -> tuple[str, str]:
        match = _S3_URL.fullmatch(self.s3_url.strip())
        if not match:
            raise S3ImportError("Expected an object URL like s3://bucket/path/file.parquet")
        return match["bucket"], match["key"]

    @property
    def suffix(self) -> str:
        return PurePosixPath(self.bucket_key()[1]).suffix.lower()

    @property
    def filename(self) -> str:
        return PurePosixPath(self.bucket_key()[1]).name


def build_user_s3_client(request: S3ImportRequest, *, allow_custom_endpoint: bool):
    """A boto3 client that can only act as the user: explicit keys, or unsigned. The default
    credential chain (environment, instance role) is never consulted."""
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config

    if request.endpoint_url and not allow_custom_endpoint:
        # A user-chosen endpoint would make the backend fetch arbitrary URLs (SSRF).
        raise S3ImportError("Custom S3 endpoints are not enabled on this deployment")
    if bool(request.access_key_id) != bool(request.secret_access_key):
        raise S3ImportError("Give both the access key id and the secret access key, or neither")

    config = Config(connect_timeout=10, read_timeout=60, retries={"max_attempts": 2})
    session = boto3.session.Session()  # no profile; keys below are the only credentials used
    if request.access_key_id:
        return session.client(
            "s3",
            region_name=request.region,
            endpoint_url=request.endpoint_url,
            aws_access_key_id=request.access_key_id,
            aws_secret_access_key=request.secret_access_key,
            aws_session_token=request.session_token or None,
            config=config,
        )
    return session.client(
        "s3",
        region_name=request.region,
        endpoint_url=request.endpoint_url,
        config=config.merge(Config(signature_version=UNSIGNED)),
    )


def download_object(client, request: S3ImportRequest, dest: Path, *, max_bytes: int) -> int:
    """Download the object into dest after checking its size; returns the bytes written."""
    from botocore.exceptions import BotoCoreError, ClientError

    bucket, key = request.bucket_key()
    if request.suffix not in (".csv", ".parquet"):
        raise S3ImportError("Only CSV and Parquet objects are supported")
    try:
        size = int(client.head_object(Bucket=bucket, Key=key)["ContentLength"])
        if size == 0:
            raise S3ImportError("The object is empty")
        if size > max_bytes:
            raise S3ImportError(f"The object is {size / 1_048_576:.0f} MB, over the {max_bytes // 1_048_576} MB limit")
        client.download_file(bucket, key, str(dest))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        messages = {
            "403": "Access denied: check the credentials, or whether the bucket is public",
            "AccessDenied": "Access denied: check the credentials, or whether the bucket is public",
            "404": "Object not found",
            "NoSuchKey": "Object not found",
            "NoSuchBucket": "Bucket not found",
            "InvalidAccessKeyId": "The access key id is not valid",
            "SignatureDoesNotMatch": "The secret access key is not valid",
        }
        raise S3ImportError(messages.get(code, f"S3 error ({code})")) from None
    except BotoCoreError as exc:
        raise S3ImportError(f"Could not reach S3: {type(exc).__name__}") from None
    written = dest.stat().st_size
    if written > max_bytes:  # the object changed after the size check
        raise S3ImportError(f"The object is over the {max_bytes // 1_048_576} MB limit")
    return written
