output "vpc_id" {
  description = "ID of the VPC."
  value       = aws_vpc.main.id
}

output "public_subnet_id" {
  description = "ID of the public subnet the node runs in."
  value       = aws_subnet.public.id
}

output "security_group_id" {
  description = "ID of the node security group."
  value       = aws_security_group.node.id
}

output "instance_id" {
  description = "EC2 instance ID of the k3s node."
  value       = aws_instance.node.id
}

output "public_ip" {
  description = "Auto-assigned public IPv4 (changes if the spot node is replaced; no EIP)."
  value       = aws_instance.node.public_ip
}

output "ssh_command" {
  description = "SSH to the node (use the private key matching var.ssh_public_key)."
  value       = "ssh ubuntu@${aws_instance.node.public_ip}"
}

output "kubeconfig_fetch" {
  description = "Fetch the k3s kubeconfig (public IP already baked into the server field)."
  value       = "scp ubuntu@${aws_instance.node.public_ip}:.kube/config ./kubeconfig-day2"
}
