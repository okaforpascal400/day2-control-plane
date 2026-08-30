# Local Environment (Phase 0)

Host: Windows 11 + WSL2 Ubuntu 26.04 LTS (amd64). Docker Engine reaches WSL through
Docker Desktop's WSL integration.

Both heavy data stores live on the external SSD (`D:`), not the system drive:

| Store | Location |
|---|---|
| WSL2 Ubuntu rootfs | `D:\wsl` |
| Docker Desktop data | `D:\DockerDesktopWSL\main` |

## Pinned toolchain

Installed to `~/.local/bin` (on `PATH` via `~/.profile`) — no root required. Every
download is SHA256-verified against the publisher's checksum file before install.

| Tool | Version |
|---|---|
| docker (client/server) | 29.1.2 |
| kubectl | v1.34.1 |
| kind | v0.32.0 |
| helm | v3.21.3 |
| terraform | v1.15.8 |
| gh | v2.96.0 |
| syft | v1.49.0 |
| trivy | v0.72.0 |

Helm is pinned to the 3.x line rather than 4.x: the upstream charts this project
consumes later (kube-prometheus-stack, Loki) are still tested against Helm 3.

## Kind cluster

`deploy/kind/cluster.yaml` defines the local cluster `day2`, with the node image
pinned by digest (`kindest/node:v1.36.1@sha256:3489c767...`) per the no-`latest` rule.

```bash
kind create cluster --config deploy/kind/cluster.yaml
kubectl --context kind-day2 get nodes
kind delete cluster --name day2
```

## Root-owned tool caches (WSL2 + containerised tools)

**Symptom.** `ruff` refuses to run from the repo root, and the message points at
a cache path rather than at any Python file:

```
ruff failed
  Cause: Failed to create temporary file
  Cause: Permission denied (os error 13) at path ".ruff_cache/0.15.22/.tmp0b2abr"
```

`trivy` fails the same way on `~/.cache/trivy/fanal/fanal.db`.

**Cause.** Not WSL, and not `sudo` on the tool itself. Anything run as
`docker run` writes to bind-mounted host paths as **uid 0**, because the
container's process is root and Docker does no uid mapping. A CI-parity local
run — the trivy step in `ci.yml` is literally `docker run -v
"${HOME}/.cache/trivy:/root/.cache/trivy"` — therefore leaves a root-owned
directory behind on the host. Once `.ruff_cache/` is mode `0755 root:root`, the
unprivileged `ruff` on `PATH` can no longer create a temp file inside it, and
`~/.cache/trivy/fanal` at `0700 root:root` is not even readable.

The trap is that `rm -rf .ruff_cache` **does not fix it**: removing the
directory's *contents* needs write permission on the root-owned directory
itself, so the cleanup needs privilege just as much as the tool did.

**Reclaim.** Use the mechanism that caused it — no interactive `sudo` needed,
which matters inside an agent or a non-tty shell:

```bash
docker run --rm -v "$PWD:/w" alpine:3.21 chown -R "$(id -u):$(id -g)" /w/.ruff_cache
docker run --rm -v "$HOME/.cache:/c" alpine:3.21 chown -R "$(id -u):$(id -g)" /c/trivy
```

`sudo chown -R "$(id -u):$(id -g)" .ruff_cache` does the same where a password
prompt is available.

**Prevention.** Any local `docker run` that bind-mounts a host path you will
later touch as your own user gets `--user`:

```bash
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/w" ...
```

This is deliberately *not* applied to `.github/workflows/*.yml`. On a GitHub
runner the workspace is disposable and the job is the only consumer, so
dropping privilege there would add a failure mode (caches the tool cannot
write) to buy nothing. The rule is for interactive use on this workstation,
where the same path is shared between a root container and a non-root shell.

Both caches are already in `.gitignore`, so a poisoned one is a local
annoyance, never something that reaches a commit.
