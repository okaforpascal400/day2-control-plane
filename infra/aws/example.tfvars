# Copy to terraform.tfvars (git-ignored) and fill in. Both required vars have
# no default. Do NOT commit your real terraform.tfvars.
#
#   cp example.tfvars terraform.tfvars
#   ssh-keygen -t ed25519 -C day2-control-plane -f ~/.ssh/day2   # if you need a key
#   curl -s https://checkip.amazonaws.com                        # your public IP

# The full OpenSSH public-key line (contents of ~/.ssh/day2.pub).
ssh_public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA... day2-control-plane"

# Your IP as a /32. Never 0.0.0.0/0 (the variable validation rejects it).
allowed_cidr = "203.0.113.7/32"

# Optional overrides (defaults shown):
# instance_type  = "t3.medium"
# x86_64 only — the GHCR images are single-arch linux/amd64, so a t4g/Graviton
# node fails every app pod with `exec format error`.
# root_volume_gb = 30
# k3s_version    = "v1.31.5+k3s1"
