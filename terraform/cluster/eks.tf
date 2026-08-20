resource "aws_eks_cluster" "this" {
  name     = var.name_prefix
  role_arn = aws_iam_role.cluster.arn
  version  = var.kubernetes_version

  vpc_config {
    subnet_ids              = aws_subnet.public[*].id
    endpoint_public_access  = true
    endpoint_private_access = true
  }

  # API_AND_CONFIG_MAP rather than API: this account operates as the root
  # user, and EKS access entries do not accept a root principal. The
  # config-map path preserves the legacy "cluster creator gets admin"
  # behaviour, which is what makes kubectl work here at all.
  access_config {
    authentication_mode                         = "API_AND_CONFIG_MAP"
    bootstrap_cluster_creator_admin_permissions = true
  }

  depends_on = [aws_iam_role_policy_attachment.cluster]
}

locals {
  # EKS appends its own nodeadm NodeConfig part to this archive, which is why
  # the launch template must not pin an AMI ID: pinning one switches EKS to
  # "custom AMI" mode where it appends nothing and the node never joins.
  user_data_mime = <<-MIME
    MIME-Version: 1.0
    Content-Type: multipart/mixed; boundary="//"

    --//
    Content-Type: text/x-shellscript; charset="us-ascii"

    ${templatefile("${path.module}/node-userdata.sh.tftpl", {
  sysctl_tuning     = var.node_sysctl_tuning
  lustre_dns        = var.enable_fsx_lustre ? aws_fsx_lustre_file_system.this[0].dns_name : ""
  lustre_mount_name = var.enable_fsx_lustre ? aws_fsx_lustre_file_system.this[0].mount_name : ""
})}

    --//--
  MIME
}

resource "aws_launch_template" "node" {
  name_prefix = "${var.name_prefix}-node-"
  # Deliberately no image_id: see the comment on user_data_mime above.
  instance_type = var.node_instance_type
  user_data     = base64encode(local.user_data_mime)

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = var.node_root_volume_gb
      volume_type           = "gp3"
      delete_on_termination = true
      encrypted             = true
    }
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  monitoring { enabled = true }

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "${var.name_prefix}-node" }
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_eks_node_group" "bench" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "bench"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = [aws_subnet.public[0].id]

  # Exactly one node. The benchmark is a node-local measurement; a second node
  # would only add cost and the risk of pods landing somewhere unexpected.
  scaling_config {
    desired_size = 1
    min_size     = 1
    max_size     = 1
  }

  launch_template {
    id      = aws_launch_template.node.id
    version = aws_launch_template.node.latest_version
  }

  labels = { "workload" = "benchmark" }

  update_config { max_unavailable = 1 }

  depends_on = [aws_iam_role_policy_attachment.node]

  lifecycle {
    ignore_changes = [scaling_config[0].desired_size]
  }
}

resource "aws_eks_addon" "this" {
  for_each = toset(["vpc-cni", "kube-proxy", "coredns"])

  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = each.value
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  depends_on = [aws_eks_node_group.bench]
}

# Lustre traffic (and nothing else) into the node.
resource "aws_vpc_security_group_ingress_rule" "node_ssh" {
  count             = var.allow_ssh_from_cidr == "" ? 0 : 1
  security_group_id = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  cidr_ipv4         = var.allow_ssh_from_cidr
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
  description       = "Manual debugging access"
}
