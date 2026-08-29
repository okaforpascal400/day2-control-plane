variable "region" {
  description = "AWS region. Fenced to ap-southeast-2 by the day2-terraform IAM policy (aws:RequestedRegion)."
  type        = string
  default     = "ap-southeast-2"
}

variable "vpc_cidr" {
  description = "CIDR for the VPC. /16 gives plenty of room; nothing else peers with it."
  type        = string
  default     = "10.0.0.0/16"
}

variable "public_subnet_cidr" {
  description = "CIDR for the single public subnet. NO private subnet, NO NAT (CLAUDE.md rule 1)."
  type        = string
  default     = "10.0.1.0/24"
}

variable "instance_type" {
  description = <<-EOT
    EC2 instance type. t3.medium = x86_64, 2 vCPU / 4 GiB. Phase 3's observability
    stack (kube-prometheus-stack + Loki + promtail) measured ~1.2 GiB on its own;
    with the app and k3s the node sits at ~2 GiB actual, which a t3.small (2 GiB,
    ~1.8 GiB allocatable) cannot hold — see deploy/observability/README.md. Drop
    back to t3.small only if the observability stack is not deployed.
    x86_64 rather than Graviton because the images CI publishes to GHCR are
    single-arch linux/amd64; the arch here must match the AMI filter in main.tf
    and the images the chart pulls.
  EOT
  type        = string
  default     = "t3.medium"
}

variable "root_volume_gb" {
  description = "Root EBS (gp3) size in GiB. 30 holds the OS, k3s, and pulled images."
  type        = number
  default     = 30
}

variable "k3s_version" {
  description = "Pinned k3s version passed to the installer as INSTALL_K3S_VERSION (CLAUDE.md rule 2: no `latest`)."
  type        = string
  default     = "v1.31.5+k3s1"
}

variable "ssh_public_key" {
  description = <<-EOT
    OpenSSH public key (the full "ssh-ed25519 AAAA... comment" line) for the
    node's login key pair. No default — must be supplied via tfvars or -var.
    Generate one with: ssh-keygen -t ed25519 -C day2-control-plane -f ~/.ssh/day2
  EOT
  type        = string
}

variable "allowed_cidr" {
  description = <<-EOT
    CIDR permitted to reach SSH (22), the k3s API (6443), and app HTTP/HTTPS
    (80/443). Lock this to your own /32 — do NOT use 0.0.0.0/0. Find yours with:
    curl -s https://checkip.amazonaws.com
  EOT
  type        = string

  validation {
    condition     = var.allowed_cidr != "0.0.0.0/0"
    error_message = "Refusing 0.0.0.0/0: least-privilege (CLAUDE.md rule 4). Scope ingress to your own IP /32."
  }
}

variable "tags" {
  description = "Tags applied to every resource in this stack."
  type        = map(string)
  default = {
    Project   = "day2-control-plane"
    Component = "aws-k3s"
    ManagedBy = "terraform"
  }
}
