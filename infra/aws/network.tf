# ---------------------------------------------------------------------------
# VPC — one public subnet, an internet gateway, and a default route to it.
# NO private subnet and NO NAT gateway (CLAUDE.md rule 1): the single node
# lives directly on the public subnet with an auto-assigned public IP.
# ---------------------------------------------------------------------------

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(var.tags, { Name = "day2-control-plane" })
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = merge(var.tags, { Name = "day2-control-plane-igw" })
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.public_subnet_cidr
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true # node gets a public IP at launch — no EIP, no NAT

  tags = merge(var.tags, { Name = "day2-control-plane-public" })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = merge(var.tags, { Name = "day2-control-plane-public" })
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# ---------------------------------------------------------------------------
# Security group — ingress scoped to var.allowed_cidr only (never 0.0.0.0/0,
# enforced by the variable validation). Egress open so the node can pull the
# k3s installer, container images, and OS packages (there is no NAT — the IGW
# route is its only path out).
# ---------------------------------------------------------------------------

resource "aws_security_group" "node" {
  name        = "day2-control-plane-node"
  description = "day2 k3s node: SSH + k3s API + app HTTP/S from allowed_cidr; egress all"
  vpc_id      = aws_vpc.main.id

  tags = merge(var.tags, { Name = "day2-control-plane-node" })
}

locals {
  ingress_ports = {
    ssh      = 22
    k3s_api  = 6443
    http     = 80
    https    = 443
  }
}

resource "aws_security_group_rule" "ingress" {
  for_each = local.ingress_ports

  type              = "ingress"
  security_group_id = aws_security_group.node.id
  protocol          = "tcp"
  from_port         = each.value
  to_port           = each.value
  cidr_blocks       = [var.allowed_cidr]
  description       = each.key
}

resource "aws_security_group_rule" "egress_all" {
  type              = "egress"
  security_group_id = aws_security_group.node.id
  protocol          = "-1"
  from_port         = 0
  to_port           = 0
  cidr_blocks       = ["0.0.0.0/0"]
  description       = "all egress (image/package pulls; IGW is the only route out)"
}
