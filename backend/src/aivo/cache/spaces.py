"""DigitalOcean Spaces (S3-compatible) mirror for public model weights.

Holds ONLY model files. Workers receive short-lived presigned GET URLs and never
see Spaces credentials. Generated content is never stored here.
"""

from __future__ import annotations

from functools import cached_property

from aivo.config import EnvSettings
from aivo_protocol.catalog_types import FileSpec


class SpacesMirror:
    def __init__(self, env: EnvSettings):
        if not (
            env.spaces_bucket and env.spaces_key and env.spaces_secret and (env.spaces_region or env.spaces_endpoint)
        ):
            raise ValueError("Spaces mirror is not configured")
        self.env = env
        self.bucket = env.spaces_bucket
        self.endpoint = env.spaces_endpoint or f"https://{env.spaces_region}.digitaloceanspaces.com"

    @cached_property
    def client(self):  # type: ignore[no-untyped-def]
        import boto3
        from botocore.config import Config

        return boto3.client(
            "s3",
            region_name=self.env.spaces_region or "us-east-1",
            endpoint_url=self.endpoint,
            aws_access_key_id=self.env.spaces_key.get_secret_value(),  # type: ignore[union-attr]
            aws_secret_access_key=self.env.spaces_secret.get_secret_value(),  # type: ignore[union-attr]
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )

    @staticmethod
    def key_for(f: FileSpec) -> str:
        return f"models/{f.repo}/{f.revision}/{f.path}"

    def presign_get(self, f: FileSpec, expires_s: int = 7200) -> str:
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": self.key_for(f)}, ExpiresIn=expires_s
        )

    def presign_put(self, f: FileSpec, expires_s: int = 7200) -> str:
        return self.client.generate_presigned_url(
            "put_object", Params={"Bucket": self.bucket, "Key": self.key_for(f)}, ExpiresIn=expires_s
        )

    def exists(self, f: FileSpec) -> int | None:
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=self.key_for(f))
            return int(head["ContentLength"])
        except Exception:
            return None

    def monthly_cost_estimate(self, stored_gb: float) -> float:
        return 5.0 + max(0.0, stored_gb - 250) * 0.02


def hf_url(f: FileSpec) -> str:
    return f"https://huggingface.co/{f.repo}/resolve/{f.revision}/{f.path}"
