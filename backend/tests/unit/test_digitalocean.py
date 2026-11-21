import base64
import gzip
import json
import uuid

import httpx
import pytest
import respx
import yaml

from aivo.config import EnvSettings
from aivo.providers.base import (
    BootImage,
    CapacityError,
    InstanceSpec,
    QuotaError,
    RateLimited,
    UnknownOutcome,
    VolumeBusy,
    tags_for,
)
from aivo.providers.digitalocean.client import classify
from aivo.providers.digitalocean.cloudinit import render_user_data
from aivo.providers.digitalocean.provider import DigitalOceanProvider, parse_offers
from aivo.worker_client.tls import make_worker_credentials
from aivo_protocol.worker_api import WorkerBootstrap

API = "https://api.digitalocean.com"

SIZES = [
    {
        "slug": "s-1vcpu-1gb",
        "memory": 1024,
        "vcpus": 1,
        "disk": 25,
        "price_hourly": 0.009,
        "regions": ["nyc1"],
        "available": True,
    },
    {
        "slug": "gpu-h100x1-80gb",
        "memory": 245760,
        "vcpus": 20,
        "disk": 720,
        "price_hourly": 4.41,
        "regions": ["nyc2", "tor1"],
        "available": True,
        "gpu_info": {"count": 1, "model": "nvidia_h100", "vram": {"amount": 80, "unit": "gib"}},
        "disk_info": [
            {"type": "local", "size": {"amount": 720, "unit": "gib"}},
            {"type": "scratch", "size": {"amount": 5, "unit": "tib"}},
        ],
    },
    {
        "slug": "gpu-l40sx1-48gb",
        "memory": 65536,
        "vcpus": 8,
        "disk": 500,
        "price_hourly": 1.57,
        "regions": [],
        "available": True,
    },  # quirk: available but no regions
    {
        "slug": "gpu-h100x8-640gb",
        "memory": 1966080,
        "vcpus": 160,
        "disk": 2046,
        "price_hourly": 35.28,
        "regions": ["nyc2"],
        "available": True,
        "gpu_info": {"count": 8, "model": "nvidia_h100", "vram": {"amount": 640, "unit": "gib"}},
    },
    {
        "slug": "gpu-mi300x1-192gb",
        "memory": 245760,
        "vcpus": 20,
        "disk": 720,
        "price_hourly": 2.59,
        "regions": ["atl1"],
        "available": True,
    },
]


def env():
    return EnvSettings(do_token="dop_v1_test", provider="digitalocean", _env_file=None)


def test_parse_offers():
    offers = parse_offers(SIZES)
    by = {(o.size_slug, o.region): o for o in offers}
    h = by[("gpu-h100x1-80gb", "nyc2")]
    assert h.gpu_class == "h100" and h.vram_gb == 80 and h.scratch_gb == 5120 and h.ram_gb == 240
    assert h.price_hourly == 4.41 and h.regions_known
    l40 = by[("gpu-l40sx1-48gb", "tor1")]
    assert not l40.regions_known and l40.gpu_class == "l40s" and l40.vram_gb == 48
    eight = by[("gpu-h100x8-640gb", "nyc2")]
    assert eight.gpu_count == 8 and eight.vram_gb == 80
    assert by[("gpu-mi300x1-192gb", "atl1")].vendor == "amd"
    assert not any(o.size_slug.startswith("s-") for o in offers)


@pytest.mark.parametrize(
    "status,msg,exc",
    [
        (422, "The size you selected is not available in this region at this time.", CapacityError),
        (422, "Droplet creation failed: GPU capacity is currently unavailable.", CapacityError),
        (422, "creating this/these droplet(s) will exceed your droplet limit", QuotaError),
        (422, "Volume is already attached to a Droplet", VolumeBusy),
        (500, "server error", UnknownOutcome),
        (429, "Too many requests", RateLimited),
    ],
)
def test_classify(status, msg, exc):
    assert isinstance(classify(status, {"id": "x", "message": msg}, creating=True), exc)


def spec():
    iid = uuid.uuid4()
    creds = make_worker_credentials(iid)
    boot = WorkerBootstrap(
        instance_id=str(iid),
        deployment_id="d",
        token_sha256="ab" * 32,
        tls_cert_pem=creds.cert_pem,
        tls_key_pem=creds.key_pem,
        worker_image="ghcr.io/x/w:1",
    )
    offer = parse_offers(SIZES)[0]
    return InstanceSpec(
        instance_id=iid,
        name="aivo-x",
        offer=offer,
        image=BootImage("gpu-h100x1-base", "base"),
        bootstrap=boot,
        tags=tags_for(uuid.uuid4(), iid),
        volume_ids=["vol-1"],
    )


def test_user_data_small_and_contains_only_bootstrap():
    sp = spec()
    ud = render_user_data(sp.bootstrap)
    assert ud.startswith("#cloud-config\n") and len(ud.encode()) < 32 * 1024
    doc = yaml.safe_load(ud[len("#cloud-config\n") :])
    boot = json.loads(base64.b64decode(doc["write_files"][0]["content"]))
    assert boot["instance_id"] == sp.bootstrap.instance_id
    assert set(boot) >= {"token_sha256", "tls_cert_pem", "tls_key_pem", "worker_image"}
    files = {f["path"]: f for f in doc["write_files"]}
    assert files["/etc/aivo/bootstrap.json"]["permissions"] == "0600"
    script = gzip.decompress(base64.b64decode(files["/opt/aivo/bootstrap.sh"]["content"])).decode()
    assert files["/opt/aivo/bootstrap.sh"]["encoding"] == "gz+b64"
    assert "169.254.169.254" in script and "tmpfs" in script and "boot_status.py" in script
    status_py = gzip.decompress(base64.b64decode(files["/opt/aivo/boot_status.py"]["content"])).decode()
    compile(status_py, "boot_status.py", "exec")


@respx.mock
async def test_create_instance_and_errors():
    p = DigitalOceanProvider(env())
    respx.get(f"{API}/v2/account/keys").mock(return_value=httpx.Response(200, json={"ssh_keys": [], "links": {}}))
    respx.post(f"{API}/v2/account/keys").mock(return_value=httpx.Response(201, json={"ssh_key": {"id": 7}}))
    created = respx.post(f"{API}/v2/droplets").mock(
        return_value=httpx.Response(
            202,
            json={
                "droplet": {
                    "id": 123,
                    "name": "aivo-x",
                    "status": "new",
                    "tags": ["aivo"],
                    "networks": {"v4": []},
                    "created_at": "2026-11-20T10:00:00Z",
                    "region": {"slug": "nyc2"},
                    "size_slug": "gpu-h100x1-80gb",
                }
            },
        )
    )
    sp = spec()
    pi = await p.create_instance(sp)
    assert pi.provider_id == "123" and pi.status == "new" and pi.public_ipv4 is None
    body = json.loads(created.calls[0].request.content)
    assert body["ssh_keys"] == [7] and body["volumes"] == ["vol-1"] and body["tags"] == sp.tags
    assert body["size"] == "gpu-h100x1-80gb" and body["image"] == "gpu-h100x1-base"

    respx.post(f"{API}/v2/droplets").mock(
        return_value=httpx.Response(
            422, json={"id": "unprocessable_entity", "message": "This size is not available in this region."}
        )
    )
    with pytest.raises(CapacityError):
        await p.create_instance(sp)
    respx.post(f"{API}/v2/droplets").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(UnknownOutcome):
        await p.create_instance(sp)
    await p.aclose()


@respx.mock
async def test_list_get_destroy_and_firewall():
    p = DigitalOceanProvider(env())
    respx.get(f"{API}/v2/droplets", params={"tag_name": "aivo-dep-1"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "droplets": [
                    {
                        "id": 5,
                        "name": "a",
                        "status": "active",
                        "tags": ["aivo-dep-1"],
                        "networks": {
                            "v4": [
                                {"type": "private", "ip_address": "10.0.0.2"},
                                {"type": "public", "ip_address": "203.0.113.5"},
                            ]
                        },
                        "created_at": "2026-11-20T10:00:00Z",
                    }
                ],
                "links": {},
            },
        )
    )
    lst = await p.list_instances("aivo-dep-1")
    assert lst[0].public_ipv4 == "203.0.113.5"
    respx.get(f"{API}/v2/droplets/9").mock(return_value=httpx.Response(404, json={"id": "not_found", "message": "x"}))
    assert await p.get_instance("9") is None
    respx.delete(f"{API}/v2/droplets/9").mock(
        return_value=httpx.Response(404, json={"id": "not_found", "message": "x"})
    )
    await p.destroy_instance("9")  # idempotent
    respx.get(f"{API}/v2/firewalls").mock(return_value=httpx.Response(200, json={"firewalls": [], "links": {}}))
    fw = respx.post(f"{API}/v2/firewalls").mock(return_value=httpx.Response(202, json={"firewall": {"id": "f"}}))
    await p.ensure_network_access("aivo-dep-1234", ["198.51.100.7/32"], 443)
    body = json.loads(fw.calls[0].request.content)
    assert body["tags"] == ["aivo-dep-1234"]
    assert body["inbound_rules"][0]["sources"]["addresses"] == ["198.51.100.7/32"]
    assert body["inbound_rules"][0]["ports"] == "443"
    await p.aclose()


@respx.mock
async def test_rate_limit_pauses_then_succeeds():
    p = DigitalOceanProvider(env())
    import time

    route = respx.get(f"{API}/v2/sizes")
    route.side_effect = [
        httpx.Response(
            429,
            json={"id": "too_many_requests", "message": "slow down"},
            headers={"ratelimit-reset": str(time.time() + 1)},
        ),
        httpx.Response(200, json={"sizes": SIZES, "links": {}}),
    ]
    offers = await p.list_offers()
    assert offers and route.call_count == 2
    await p.aclose()


@respx.mock
async def test_base_image_discovery():
    p = DigitalOceanProvider(env())
    respx.get(f"{API}/v2/images").mock(
        return_value=httpx.Response(
            200,
            json={
                "images": [{"slug": "ubuntu-24-04-x64"}, {"slug": "gpu-h100x1-base"}, {"slug": "gpu-amd-base"}],
                "links": {},
            },
        )
    )
    o = parse_offers(SIZES)
    nv = await p.boot_image(next(x for x in o if x.vendor == "nvidia"), "auto")
    amd = await p.boot_image(next(x for x in o if x.vendor == "amd"), "auto")
    assert nv.ref == "gpu-h100x1-base" and amd.ref == "gpu-amd-base"
    await p.aclose()


def test_bake_user_data_runs_the_self_test_with_python_and_forgets_its_key():
    from aivo.providers.digitalocean.cloudinit import render_bake_user_data

    sp = spec()
    ud = render_bake_user_data(sp.bootstrap)
    assert len(ud.encode()) < 32 * 1024
    files = {f["path"]: f for f in yaml.safe_load(ud[len("#cloud-config\n") :])["write_files"]}
    assert set(files) == {"/etc/aivo/bootstrap.json", "/opt/aivo/bake.sh", "/opt/aivo/boot_status.py"}
    script = gzip.decompress(base64.b64decode(files["/opt/aivo/bake.sh"]["content"])).decode()
    # the worker image's entrypoint is the agent: the GPU self-test must run python explicitly
    assert "--entrypoint python" in script
    assert "rm -rf /etc/aivo" in script and script.rstrip().endswith("poweroff")
