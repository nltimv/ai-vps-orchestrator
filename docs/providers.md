# Adding a cloud provider

Everything cloud-specific lives behind `aivo.providers.base.CloudProvider`. The scheduler,
provisioner and reconciler only use this interface.

## Required methods

| Method | Contract |
|---|---|
| `list_offers()` | Normalized `GpuOffer`s: one per GPU size × region where it can be created *now*, with hourly price, VRAM per GPU, RAM, disk, scratch disk and vendor. |
| `regions()` | `RegionMeta` (name, coordinates, a host for latency probes). |
| `boot_image(offer, "base")` | The vendor's GPU-ready base image. The backend picks prepared snapshots from its own cache table. |
| `create_instance(spec)` | Create the machine with `spec.tags`, bootstrap data (`spec.bootstrap`, delivered however the cloud supports, e.g. cloud-init) and optional cache volumes. |
| `get_instance(id)` / `list_instances(tag)` | Return `ProviderInstance`; `None` if it's gone. Listing **by tag** is what makes idempotent creates and orphan sweeps possible. |
| `destroy_instance(id)` | Idempotent; a missing machine counts as success. |
| `ensure_network_access(tag, cidrs, port)` | Restrict inbound worker traffic; a no-op is acceptable (TLS pinning still protects). |
| `account_month_to_date_usd()`, `gpu_quota()` | Best effort; `None` if unknown. |

Map errors to the shared taxonomy. Fallback and idempotency depend on it:

| Error | Meaning |
|---|---|
| `CapacityError` | This size/region is sold out; the next candidate is tried |
| `QuotaError` | Account limit reached |
| `VolumeBusy` | Retry without the cache volume |
| `RateLimited(reset_at)` | Pause, don't fail |
| `UnknownOutcome` | The request may have created a machine; the caller looks it up by tag instead of retrying |
| `AuthError` / `ConfigError` | Configuration problem; surfaced to the admin |
| `TransientError` | Retry later |

## Traits
A provider that isn't "a machine billed from create to delete, reached over pinned TLS"
registers `ProviderTraits` (`providers/base.py`). RunPod Serverless uses them:

- `bills_from_boot`, `min_billing_s`: billing starts when the worker starts, with no minimum.
  Budget accrual and the scorer's cost estimates follow.
- `max_idle_s`: idle machines are let go sooner than the general idle timeout.
- `prepare_timeout_s`: how long the provider may prepare a machine (free) before aivo moves on.
- `firewall`, `machine_images`: skip firewall rules, prepared images and model disks.
- `pinned_tls=False`: the worker sits behind the provider's HTTPS gateway.
  `get_instance` returns `address` (the gateway URL) instead of an IP. The backend then uses
  `WorkerClient.via_gateway` (token in `X-Aivo-Token`, requests pinned to one worker, outputs
  fetched in ranges below the gateway's body limit).

`get_instance` may also report `stage`/`note`, the step before the machine runs, shown on the
machine card, and `failed` for a definitive start-up failure.

## Optional capabilities
These unlock the "Faster start-up" features (`hasattr` checks):
- `create_bake_instance`, `snapshot`, `wait_action`, `find_snapshot`, `transfer_image`,
  `delete_image`: prepared images.
- `create_volume`, `delete_volume`, `create_utility_instance`: model disks.

## Steps
1. Implement `aivo/providers/<cloud>/provider.py`, using DigitalOcean's as a template, and
   register it in `providers/registry.py`.
2. Add GPU name aliases for the cloud's sizes to `catalog/gpu_classes.yaml`.
3. Write HTTP-mocked tests modelled on `backend/tests/unit/test_digitalocean.py`.
4. The full integration suite runs against `FakeProvider`, so the shared logic is already
   covered.
