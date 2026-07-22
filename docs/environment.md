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
