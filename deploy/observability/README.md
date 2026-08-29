# observability — metrics, logs, dashboards, alerts

Phase 3. A single Helm release (`obs`, namespace `monitoring`) that layers
observability over the same app the `deploy/helm` chart runs on Kind and k3s:

- **kube-prometheus-stack** `88.6.1` — Prometheus, Grafana, Alertmanager,
  node-exporter, kube-state-metrics, the prometheus-operator.
- **loki** `7.3.0` — single-binary, filesystem store, 48h retention.
- **promtail** `6.17.1` — one DaemonSet pod shipping all container logs to Loki.

Subcharts are pinned in `Chart.yaml` / `Chart.lock` (CLAUDE.md rule 2). The `.tgz`
archives under `charts/` are gitignored; fetch them with `helm dependency build`.

## What Phase 3 adds to the app

The api and worker now expose Prometheus `/metrics` (chart `deploy/helm` ≥ 0.2.0):

| Service | Endpoint | Key metrics |
|---|---|---|
| api | `:8000/metrics` (via `prometheus-fastapi-instrumentator`) | `http_requests_total{handler,status,method}`, `http_request_duration_seconds_*` |
| worker | `:9000/metrics` (via `prometheus-client`) | `day2_job_queue_depth{status}`, `day2_jobs_processed_total{result}`, `day2_job_processing_seconds_*` |

Scraped by a `ServiceMonitor` (api) and `PodMonitor` (worker) that the app chart
renders when `monitoring.enabled=true` (set in `values-local.yaml`/`values-aws.yaml`).

## Install

Common to both: the Grafana admin credential is a Secret created **out of band**,
never in git — the same pattern as the Postgres password.

```sh
helm dependency build deploy/observability
kubectl create namespace monitoring
kubectl -n monitoring create secret generic grafana-admin \
  --from-literal=admin-user=admin \
  --from-literal=admin-password="$(openssl rand -base64 24)"
```

Kind:

```sh
helm upgrade --install obs deploy/observability -n monitoring \
  -f deploy/observability/values-local.yaml
```

k3s (with `KUBECONFIG=./kubeconfig-day2` fetched per `infra/aws/README.md`):

```sh
helm upgrade --install obs deploy/observability -n monitoring \
  -f deploy/observability/values-aws.yaml
```

Then turn on app scraping by deploying the app chart with monitoring enabled
(`values-local.yaml` / `values-aws.yaml` already set `monitoring.enabled: true`).

## Reaching Grafana — port-forward, no new ports

Grafana stays off the public internet. Reach it over `kubectl port-forward`
(which tunnels through the k3s API on 6443, already open to `allowed_cidr` — **no
new security-group rule**):

```sh
kubectl -n monitoring port-forward svc/obs-grafana 3000:80
# browse http://localhost:3000  (user admin, password from the Secret above)
```

## Dashboards (curated, folder "day2")

Authored as JSON in `dashboards/`, wrapped into labelled ConfigMaps by
`templates/dashboards.yaml`, and imported by the Grafana sidecar. Edit the JSON
to change a dashboard — no template edits.

- **App overview** — API request rate / p50-p95-p99 latency / 5xx ratio, job
  queue depth by status, drain rate, job processing time, and a Loki logs panel.
- **Cluster health** — pod phase/restarts, containers waiting, per-pod CPU and
  memory (cAdvisor + kube-state-metrics).
- **Node resources** — CPU / memory / disk / load / network / disk-IO
  (node-exporter).

The stack's ~30 bundled dashboards are turned **off** (`defaultDashboardsEnabled:
false`) — they cost Grafana memory and bury the signal behind the three curated ones.

## Alerts

`templates/prometheus-rules.yaml` — a small, legible set (the stack's ~100 default
rules are off):

| Alert | Fires when |
|---|---|
| `PodCrashLooping` | a container is in CrashLoopBackOff for 5m |
| `NodeMemoryPressure` | node memory > 85% for 10m |
| `NodeDiskAlmostFull` | root filesystem > 85% for 10m |
| `JobQueueStuck` | > 10 jobs pending **and** zero completions for 10m |
| `HighApiErrorRate` | API 5xx ratio > 5% for 5m |

Alertmanager is installed with a null receiver (the alerts are the deliverable;
wiring a real notifier is out of Phase 3 scope).

## Load generator

`scripts/loadgen.py` (stdlib only) drives a realistic traffic mix so the
dashboards show life. See its header for local / cloud / in-cluster invocations.

## Resource footprint & node sizing

Measured on Kind under load (`container_memory_working_set_bytes`):

| Group | Working set |
|---|---|
| observability (this release) | **~1.18 GiB** |
| app (api×1, worker, web, postgres) | ~0.24 GiB |
| Grafana / Prometheus / Loki (the three heaviest) | ~300 / 273 / 228 MiB |

Projected onto the k3s node — observability **~1.18 GiB** + app **~0.30 GiB**
(api runs 2 replicas on AWS) + k3s itself **~0.5 GiB** ≈ **~2.0 GiB actual**.

**A `t3.small` (2 GiB, ~1.8 GiB allocatable) does not fit this.** `infra/aws/
variables.tf` already anticipates it: *"Bump to t3.medium (4 GiB) before Phase 3
lands the observability stack."* The instance type lives in `infra/aws`
(`var.instance_type`); this chart only sizes the workloads. Because a t3.medium
run 24/7 (~$17–19/mo) would exceed the ~$15 ceiling, the node stays **ephemeral**
— brought up to verify, then destroyed.

## AWS image delivery (0.2.0)

The api/worker images changed (they now serve `/metrics`), so the `0.1.0` digests
pinned in `deploy/helm/values-aws.yaml` predate the metrics. Two paths:

- **Steady state:** once this PR merges, CI republishes the `0.2.0` images; re-pin
  the three digests in `values-aws.yaml` (see the note there).
- **Pre-merge verification:** side-load the freshly built images straight into the
  node's containerd, the cloud analogue of `kind load`:

  ```sh
  for s in api worker web; do
    docker save "day2/$s:0.2.0" | gzip | \
      ssh ubuntu@<public-ip> "sudo k3s ctr images import -"
  done
  ```

  then deploy with the app image references pointed at those local tags.
