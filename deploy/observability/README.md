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

Measured on Kind under load, then **re-measured on the k3s node** during the
2026-08-29 cloud arc (`max_over_time(container_memory_working_set_bytes[15m])`
across the 10-minute load run):

| Group | Kind (projected) | k3s node (measured) |
|---|---|---|
| observability (this release) | ~1.18 GiB | **949 MiB** |
| app (api×2, worker, web, postgres) | ~0.30 GiB | **308 MiB** |
| Grafana / Prometheus / Loki | ~300 / 273 / 228 MiB | **438 / 227 / 173 MiB** |

Node totals under load: **2.40 GiB peak used of 3.75 GiB** allocatable, with
**1.64 GiB / 0.890 cores** requested by the scheduler out of 3.75 GiB / 2 vCPU.
The Kind projection over-estimated the stack by ~230 MiB; the real footprint is
smaller, but the conclusion is unchanged and now measured rather than inferred:

**A `t3.small` (2 GiB, ~1.8 GiB allocatable) does not fit this** — peak usage
alone (2.40 GiB) exceeds a t3.small's entire RAM. `infra/aws/variables.tf`
therefore defaults `var.instance_type` to **`t3.medium`** (4 GiB); the instance
type lives in `infra/aws`, and this chart only sizes the workloads. Because a
t3.medium run 24/7 (~$17–19/mo) would exceed the ~$15 ceiling, the node stays
**ephemeral** — brought up to verify, then destroyed.

## Cloud (k3s) deploy — verified 2026-08-29

Both the Kind and the AWS paths are verified. The AWS arc ran
`apply → deploy → load → verify → destroy` in **80 minutes for ~$0.03** (t3.medium
spot at $0.0200–0.0203/hr in ap-southeast-2, plus a prorated 30 GiB gp3 root).

The drift-tolerant approach worked as designed: the **node** pulled the `0.2.0`
images from GHCR over its own egress, so the operator's link carried only short
`kubectl`/`helm` calls over 6443 — no multi-hundred-MB SSH image transfer, and
nothing that a mid-arc IP change could break. What was recorded:

| Check | Result |
|---|---|
| Node | `t3.medium` spot, Ubuntu 22.04.5, k3s `v1.31.5+k3s1` |
| Images | all three `0.2.0` GHCR digests, pulled by the node itself |
| Pods | 5 app + 8 observability Ready, **0 restarts** |
| Scrape targets | 3 day2 targets up (api ×2 + worker); 16/16 active targets healthy |
| Load | 17,946 requests / 600 s at 30 rps — 17,452 2xx, 494 4xx, 0 connection errors |
| API latency | p50 5.09 ms · p95 9.66 ms · p99 16.2 ms |
| Job queue | peaked ~2,110 pending, drained at ~3.9 jobs/s |
| Logs | Loki carried streams from api, worker, web and postgres |
| Dashboards | all three imported into folder `day2`; every panel query returned data through Grafana's own datasource proxy |
| Alerting | `JobQueueStuck` went inactive → pending → **firing**, reached Alertmanager, and resolved on recovery |
| Teardown | `terraform destroy` removed all 13 resources; state empty and an API sweep matched the pre-apply baseline exactly |

**Install order matters.** `deploy/helm/values-aws.yaml` sets
`monitoring.enabled: true`, so the app chart renders a `ServiceMonitor` and a
`PodMonitor`. Those CRDs ship with *this* release, so install
`deploy/observability` **first** — otherwise the app install aborts with
`no matches for kind "ServiceMonitor" in version "monitoring.coreos.com/v1"`.

Two dashboard defects surfaced only under real load and were fixed in the same PR:

- **Latency percentiles** queried `http_request_duration_seconds_bucket`, whose
  `prometheus-fastapi-instrumentator` default buckets are `(0.1, 0.5, 1)`. The api
  answers in 5–30 ms, so every observation landed in the first bucket and
  `histogram_quantile` returned the constants 0.05 / 0.095 / 0.099 regardless of
  actual latency. Now on `http_request_duration_highr_seconds_bucket` (21 buckets
  from 0.01 s), which is the histogram the library documents for percentiles.
- **The 5xx ratio panel** rendered "No data" instead of `0` on a healthy service,
  because no `status="5xx"` series exists at all and an empty numerator makes the
  whole expression empty. Wrapped as `(... or vector(0))`.

Alertmanager still uses a null receiver — the alerts and their firing path are the
Phase 3 deliverable; wiring a real notifier is out of scope.

The alert was exercised by holding a Postgres row lock over the pending jobs
(`SELECT ... FOR UPDATE` in an open transaction). The worker's
`FOR UPDATE SKIP LOCKED` claim then returned nothing, so it stayed healthy and kept
publishing an accurate queue-depth gauge while completions stopped — a faithful
"long transaction starves the worker" incident. Note that simply scaling the worker
to zero does **not** work: `day2_job_queue_depth` is a worker metric, so its series
would disappear and the alert could never satisfy its own first condition.

## Operational defect, found in Phase 6: the stack was not watching its own collector

Recorded here rather than in the Phase 6 notes, because it is a defect in *this*
phase's work — Phase 3 was marked complete with it already present.

**What happened.** While building the Observability Copilot, its `search_logs`
tool returned zero lines against a cluster that was plainly running. Loki held no
labels at all. `promtail` was `0/1 Running` and had been for roughly **45 hours**.
Nothing alerted, and nothing looked wrong: metrics kept flowing, all three
dashboards stayed populated, and Phase 3's "3/3 scrape targets up" stayed true
the whole time — because those three targets are the *application* services. The
log pipeline was never in the count.

**Cause.** Promtail was saturating its own CPU, measured at **1.43 cores** against
a `20m` request with no limit. At that load even its local `/ready` probe timed
out (`Readiness probe failed ... context deadline exceeded`, x1616 over 4h16m),
and so did every push to Loki (`context deadline exceeded` on
`/loki/api/v1/push`). Deleting the pod cleared it: `1/1 Ready`, CPU
**1.43 → 0.06 cores**, 11 labels in Loki within a minute.

**Why it went unnoticed for 45 hours.** The pod stayed `Running`, so
`PodCrashLooping` never fired — that rule keys on `CrashLoopBackOff`. Nothing
else looked at readiness. This is the worst shape of observability failure: an
empty log query is indistinguishable from "nothing was logged", so the gap
argues for its own absence.

**Fixed in this branch.** A `day2.observability` rule group with
`PromtailNotReady` and `LokiNotReady`, comparing *ready* against *desired* rather
than testing `== 0`, so a partial outage on a multi-node cluster also fires.

**Verified against the recorded history rather than asserted.** The new
expression was evaluated at past timestamps spanning the real outage:

| Evaluated at | `PromtailNotReady` |
|---|---|
| 2026-08-30T18:00Z | **FIRING** (ready=0) |
| 2026-08-30T19:00Z | **FIRING** (ready=0) |
| 2026-08-30T20:15Z | **FIRING** (ready=0) |
| 2026-08-30T20:25Z (post-restart) | not firing |
| 2026-08-30T20:45Z | not firing |

**Still open — promtail and Loki are not scraped at all.** `up{job=~".*promtail.*"}`
and `up{job=~".*loki.*"}` both return zero series, so their own metrics
(`promtail_sent_entries_total`, `loki_distributor_lines_received_total`) do not
exist in Prometheus. The new rules therefore work from kube-state-metrics
readiness, which catches the outage that actually happened but would **not**
catch a promtail that is Ready while silently dropping lines. Closing that needs
ServiceMonitors for both, which is a Phase 3 change and is left as recorded debt
rather than widened into the copilot's branch.

### Adjacent finding: two rules reference series that are absent, not zero

Noticed while validating the new rules against live Prometheus, and recorded
rather than fixed — it is Phase 3 alert debt, not copilot work.

`day2_jobs_processed_total` and `http_requests_total` both return **0 series**
today. The first is genuinely defined in `app/worker/worker/metrics.py` as a
`Counter`, but a labelled Prometheus counter does not exist until it has been
incremented once: a worker that has restarted and not yet completed a job
exports nothing for it.

That matters for `JobQueueStuck`, whose second condition is
`sum(rate(day2_jobs_processed_total{result="completed"}[10m])) == 0`. Against an
absent series that expression yields an **empty vector, not `true`**, and
`A and B` with an empty `B` is empty — so the alert cannot fire. The case it
silently misses is the worst one: a worker that is crash-looping and has never
processed a job since starting. Phase 3's live test passed because the worker
there was healthy and *had* completed jobs, so the counter existed before the
row lock stopped it.

`HighApiErrorRate` has the same shape against `http_requests_total`, which the
API does not expose at all — it publishes `http_request_duration_highr_seconds_*`.

The fix in both cases is `or vector(0)` around the counter half (the pattern
already used elsewhere in this file), plus using a series the API actually
exports. Left as recorded debt.
