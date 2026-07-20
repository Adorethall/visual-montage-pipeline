from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import boto3
import httpx
from botocore.client import Config


class StorageClient:
    def __init__(self) -> None:
        self.endpoint = os.environ["MINIO_ENDPOINT"]
        self.public_endpoint = os.getenv("MINIO_PUBLIC_ENDPOINT") or self.endpoint
        self.access_key = os.environ["MINIO_ACCESS_KEY"]
        self.secret_key = os.environ["MINIO_SECRET_KEY"]
        self.bucket = os.getenv("MINIO_BUCKET", "media")
        self.region = os.getenv("MINIO_REGION", "us-east-1")
        self._client = self._make_client(self.endpoint)
        self._public_client = self._make_client(self.public_endpoint)

    def _make_client(self, endpoint: str):
        return boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name=self.region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                proxies={},
            ),
        )

    def upload_for_worker(self, source: Path, key: str, expires_in: int) -> dict[str, str]:
        self._client.upload_file(str(source), self.bucket, key)
        url = self._public_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_in,
        )
        return {"object_path": f"s3://{self.bucket}/{key}", "public_url": url}

    def download_url(self, url: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with httpx.stream("GET", url, timeout=300) as response:
            response.raise_for_status()
            with destination.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)

    def download_object_path(self, object_path: str, destination: Path) -> None:
        if not object_path.startswith("s3://"):
            raise ValueError(f"unsupported object path: {object_path}")
        bucket, key = object_path[5:].split("/", 1)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._client.download_file(bucket, key, str(destination))

    def download_result(
        self,
        *,
        public_url: str | None,
        object_path: str | None,
        destination: Path,
    ) -> None:
        if public_url:
            self.download_url(public_url, destination)
        elif object_path:
            self.download_object_path(object_path, destination)
        else:
            raise ValueError("remote result has no downloadable path")


@lru_cache(maxsize=1)
def get_storage() -> StorageClient:
    return StorageClient()
