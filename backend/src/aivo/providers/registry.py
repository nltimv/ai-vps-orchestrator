from __future__ import annotations

from aivo.config import EnvSettings
from aivo.providers.base import CloudProvider


def make_provider(env: EnvSettings) -> CloudProvider:
    if env.provider == "fake":
        from aivo.providers.fake.provider import FakeProvider

        return FakeProvider(
            boot_seconds=env.fake_boot_seconds,
            job_seconds=env.fake_job_seconds,
            chaos={"*": list(env.fake_chaos)} if env.fake_chaos else {},
        )
    if env.provider == "digitalocean":
        from aivo.providers.digitalocean.provider import DigitalOceanProvider

        if env.do_token is None:
            raise ValueError("AIVO_DO_TOKEN is required for the digitalocean provider")
        return DigitalOceanProvider(env)
    raise ValueError(f"unknown provider {env.provider}")
