"""S3 import, offline: URL parsing, client construction, size checks and the route's guards."""
from __future__ import annotations

from pathlib import Path

import pytest
from botocore import UNSIGNED
from botocore.exceptions import ClientError

from app.dependencies import get_jammi_provisioner, get_jammi_upload_store
from app.services.jammi_sources import JammiSourceProvisioner
from app.services.jammi_uploads import LocalUploadStore
from app.services.s3_import import S3ImportError, S3ImportRequest, build_user_s3_client, download_object

from test_connections_route import _build_connection_app
from test_jammi_connection_routes import FakeJammi


@pytest.mark.parametrize("url", ["s3://b", "https://bucket.s3.amazonaws.com/x.csv", "s3://Bad_Bucket/x.csv", "s3://bucket/"])
def test_malformed_urls_are_refused(url):
    with pytest.raises(S3ImportError):
        S3ImportRequest(s3_url=url).bucket_key()


def test_url_parts():
    request = S3ImportRequest(s3_url="s3://my-bucket/exports/2024/Sales Q1.CSV")
    assert request.bucket_key() == ("my-bucket", "exports/2024/Sales Q1.CSV")
    assert request.suffix == ".csv" and request.filename == "Sales Q1.CSV"


def test_without_keys_the_client_is_unsigned_even_with_backend_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIABACKEND")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "backend-secret")
    client = build_user_s3_client(S3ImportRequest(s3_url="s3://bkt/x.csv"), allow_custom_endpoint=False)
    assert client.meta.config.signature_version == UNSIGNED


def test_with_keys_the_client_uses_exactly_those_keys(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIABACKEND")
    client = build_user_s3_client(
        S3ImportRequest(s3_url="s3://bkt/x.csv", access_key_id="AKIAUSER", secret_access_key="user-secret"),
        allow_custom_endpoint=False,
    )
    assert client._request_signer._credentials.access_key == "AKIAUSER"


def test_custom_endpoint_and_half_keys_are_refused():
    with pytest.raises(S3ImportError, match="not enabled"):
        build_user_s3_client(S3ImportRequest(s3_url="s3://bkt/x.csv", endpoint_url="http://169.254.169.254"),
                             allow_custom_endpoint=False)
    with pytest.raises(S3ImportError, match="both"):
        build_user_s3_client(S3ImportRequest(s3_url="s3://bkt/x.csv", access_key_id="AKIAUSER"),
                             allow_custom_endpoint=False)


class FakeS3:
    def __init__(self, size=10, error_code=None, body=b"a,b\n1,2\n"):
        self.size, self.error_code, self.body, self.downloaded = size, error_code, body, False

    def head_object(self, Bucket, Key):
        if self.error_code:
            raise ClientError({"Error": {"Code": self.error_code, "Message": "x"}}, "HeadObject")
        return {"ContentLength": self.size}

    def download_file(self, bucket, key, dest):
        self.downloaded = True
        Path(dest).write_bytes(self.body)


def test_size_is_checked_before_downloading(tmp_path: Path):
    s3 = FakeS3(size=11)
    with pytest.raises(S3ImportError, match="limit"):
        download_object(s3, S3ImportRequest(s3_url="s3://bkt/x.csv"), tmp_path / "x.csv", max_bytes=10)
    assert not s3.downloaded


def test_object_growing_after_the_size_check_is_refused(tmp_path: Path):
    s3 = FakeS3(size=5, body=b"x" * 50)
    with pytest.raises(S3ImportError, match="limit"):
        download_object(s3, S3ImportRequest(s3_url="s3://bkt/x.csv"), tmp_path / "x.csv", max_bytes=10)


@pytest.mark.parametrize("code,message", [("403", "Access denied"), ("NoSuchKey", "not found"),
                                          ("SignatureDoesNotMatch", "secret access key")])
def test_s3_errors_become_user_messages(tmp_path: Path, code, message):
    with pytest.raises(S3ImportError, match=message):
        download_object(FakeS3(error_code=code), S3ImportRequest(s3_url="s3://bkt/x.csv"), tmp_path / "x", max_bytes=10)


def test_route_requires_jammi_and_storage(tmp_path: Path):
    client, _, _ = _build_connection_app(tmp_path)
    response = client.post("/connections/s3-import", json={"s3_url": "s3://bkt/x.csv", "display_name": "x"})
    assert response.status_code == 400 and response.json()["detail"].startswith("S3_IMPORT_REQUIRES_JAMMI")


def test_route_refuses_unsupported_objects_before_any_request(tmp_path: Path):
    client, _, _ = _build_connection_app(tmp_path)
    client.app.dependency_overrides[get_jammi_provisioner] = lambda: JammiSourceProvisioner(FakeJammi())
    client.app.dependency_overrides[get_jammi_upload_store] = lambda: LocalUploadStore(tmp_path / "u", "/u")
    response = client.post("/connections/s3-import", json={"s3_url": "s3://bkt/x.xlsx", "display_name": "x"})
    assert response.status_code == 400 and "CSV and Parquet" in response.json()["detail"]
