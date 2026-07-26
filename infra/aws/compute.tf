# ---------------------------------------------------------------------------
# Login key pair (public half only; the private half never touches AWS or git).
# ---------------------------------------------------------------------------

resource "aws_key_pair" "node" {
  key_name   = "day2-control-plane"
  public_key = var.ssh_public_key
  tags       = merge(var.tags, { Name = "day2-control-plane" })
}

# ---------------------------------------------------------------------------
# Spot EC2 node running k3s.
#
# Spot via instance_market_options (RunInstances) — NOT a spot fleet — so the
# day2-terraform policy needs only RunInstances, not RequestSpotFleet. No
# max_price: capped at the on-demand price, so it never overpays but also
# won't outbid a demand spike. No instance profile: the policy grants no IAM
# actions (rule 4), and Phase 2 needs none.
# ---------------------------------------------------------------------------

resource "aws_instance" "node" {
  ami           = data.aws_ami.ubuntu.id
  instance_type = var.instance_type
  subnet_id     = aws_subnet.public.id
  key_name      = aws_key_pair.node.key_name

  vpc_security_group_ids = [aws_security_group.node.id]

  instance_market_options {
    market_type = "spot"
    spot_options {
      spot_instance_type             = "one-time"
      instance_interruption_behavior = "terminate"
    }
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_gb
    encrypted             = true
    delete_on_termination = true
  }

  metadata_options {
    http_tokens   = "required" # IMDSv2 only
    http_endpoint = "enabled"
  }

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    k3s_version = var.k3s_version
  })

  # A new AMI or user_data must replace the node, not update in place.
  user_data_replace_on_change = true

  tags = merge(var.tags, { Name = "day2-control-plane-k3s" })
}
