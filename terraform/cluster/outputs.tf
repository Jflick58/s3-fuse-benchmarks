output "cluster_name" {
  value = aws_eks_cluster.this.name
}

output "region" {
  value = var.region
}

output "bucket" {
  value = data.terraform_remote_state.data.outputs.bucket
}

output "ecr_repository_url" {
  value = aws_ecr_repository.bench.repository_url
}

output "node_instance_type" {
  value = var.node_instance_type
}

output "fsx_enabled" {
  value = var.enable_fsx_lustre
}

output "fsx_dns_name" {
  value = var.enable_fsx_lustre ? aws_fsx_lustre_file_system.this[0].dns_name : null
}

output "fsx_mount_name" {
  value = var.enable_fsx_lustre ? aws_fsx_lustre_file_system.this[0].mount_name : null
}

output "kubeconfig_command" {
  description = "Run this to point kubectl at the benchmark cluster."
  value       = "aws eks update-kubeconfig --name ${aws_eks_cluster.this.name} --region ${var.region} --profile ${var.aws_profile}"
}
