# Versions and upgrades

## How versions are made

Every push to `main` runs the **release** workflow (`.github/workflows/images.yml`). It builds
everything under one version:

```
<VERSION file>.<build number>      e.g. 0.2.57
```

- `VERSION` (repository root) holds `MAJOR.MINOR`. Edit it to start a new series, e.g. `0.3`.
- The build number is the release workflow's run number. It only goes up, so every release has a
  higher version than the one before, also across a `VERSION` bump.
- Re-running a failed release keeps its version. A manual run from another branch makes a
  pre-release such as `0.2.58-my-branch`, which sorts below `0.2.58`.

Each release publishes:

| What | Where |
|---|---|
| Images | `ghcr.io/nltimv/aivo-backend`, `aivo-frontend`, `aivo-worker`, `aivo-worker-mock`, tagged `<version>` (plus `latest` and `sha-<commit>` on `main`) |
| Helm chart | `oci://ghcr.io/nltimv/charts/ai-vps-orchestrator`, `version` = `appVersion` = `<version>` |
| Git tag | `v<version>` on the released commit |

The chart is published only after all its images are pushed. The version is baked into the
images (`AIVO_VERSION`). Settings → Status and `/api/v1/health` show it.

## What an upgrade changes

The chart runs exactly the images of its own release, so a new chart version always brings new
image tags:

- **Backend and controller**: a new image tag means new pods. The `migrate` init container
  updates the database before the new backend starts. Pods that run the controller are replaced
  (`Recreate`), never doubled, so only one controller runs at a time.
- **Dashboard**: the frontend image of the same release.
- **RunPod workers**: the backend updates its RunPod endpoints to the worker image of its own release
  (`aivo-worker:<version>`), and RunPod replaces their workers with the new release. Worker and backend
  check each other's protocol version, so an older worker can't be given work it doesn't understand.
  RunPod hosts that have an older release pull only the layers that changed.

Version tags never change once pushed, so Kubernetes keeps the default pull policy for them
(pull once). `latest` is always pulled again.

## Upgrading

Keep your settings in a values file and name the release:

```bash
helm upgrade --install aivo oci://ghcr.io/nltimv/charts/ai-vps-orchestrator \
  --version 0.2.61 -f my-values.yaml
```

- **Which versions exist:** `helm show chart oci://ghcr.io/nltimv/charts/ai-vps-orchestrator`
  (newest), or the repository's Packages page on GitHub.
- **Without a values file:** `--reset-then-reuse-values` keeps what you set before and takes the
  new chart's defaults for anything new. Don't use plain `--reuse-values` across chart versions:
  it ignores new defaults.
- **Rolling back:** `helm rollback aivo` goes back to the previous release's images. The
  database stays upgraded. So far every migration only adds columns and tables, which the
  previous backend ignores.
- **Argo CD / Flux:** point them at the OCI chart and pin `targetRevision` / `version`, or allow
  a range such as `0.2.*` to follow new builds automatically.

The first time the chart package is published it may be private (GitHub → your profile →
Packages → `charts/ai-vps-orchestrator` → Package settings). Make it public, or log in with
`helm registry login ghcr.io` before installing.

## Pinning or overriding

| Value | Effect |
|---|---|
| `image.backend.tag`, `image.frontend.tag` | Run other images than the chart's release |
| `image.worker.tag` | Worker image tag RunPod runs (default: the chart's release) |
| `workerImage` | Full worker image reference, e.g. a fork's (`AIVO_WORKER_IMAGE`) |
| `image.*.pullPolicy` | Empty: Kubernetes decides (pull once for versions, always for `latest`) |

## Development

- The chart in this repository (`deploy/helm/ai-vps-orchestrator`, version `0.2.0-dev`) runs the
  `latest` images. Use it for trying changes, not for upgrades: a chart that keeps the same
  `latest` tag can't tell Kubernetes that anything changed.
- A source checkout or `docker compose` build reports version `dev` and uses
  `aivo-worker:latest` for RunPod workers.
