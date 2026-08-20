resource "aws_security_group" "fsx" {
  count       = var.enable_fsx_lustre ? 1 : 0
  name        = "${var.name_prefix}-fsx"
  description = "FSx for Lustre traffic"
  vpc_id      = aws_vpc.this.id
  tags        = { Name = "${var.name_prefix}-fsx" }
}

# Lustre uses 988 for the management/LNet channel and 1018-1023 for the
# per-target connections. Both directions are required between the clients
# and the filesystem's network interfaces.
resource "aws_vpc_security_group_ingress_rule" "fsx_from_nodes" {
  for_each = var.enable_fsx_lustre ? {
    mgmt    = { from = 988, to = 988 }
    targets = { from = 1018, to = 1023 }
  } : {}

  security_group_id            = aws_security_group.fsx[0].id
  referenced_security_group_id = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  from_port                    = each.value.from
  to_port                      = each.value.to
  ip_protocol                  = "tcp"
  description                  = "Lustre ${each.key} from benchmark nodes"
}

resource "aws_vpc_security_group_ingress_rule" "fsx_self" {
  for_each = var.enable_fsx_lustre ? {
    mgmt    = { from = 988, to = 988 }
    targets = { from = 1018, to = 1023 }
  } : {}

  security_group_id            = aws_security_group.fsx[0].id
  referenced_security_group_id = aws_security_group.fsx[0].id
  from_port                    = each.value.from
  to_port                      = each.value.to
  ip_protocol                  = "tcp"
  description                  = "Lustre ${each.key} intra-filesystem"
}

resource "aws_vpc_security_group_egress_rule" "fsx_all" {
  count             = var.enable_fsx_lustre ? 1 : 0
  security_group_id = aws_security_group.fsx[0].id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_vpc_security_group_ingress_rule" "nodes_from_fsx" {
  for_each = var.enable_fsx_lustre ? {
    mgmt    = { from = 988, to = 988 }
    targets = { from = 1018, to = 1023 }
  } : {}

  security_group_id            = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  referenced_security_group_id = aws_security_group.fsx[0].id
  from_port                    = each.value.from
  to_port                      = each.value.to
  ip_protocol                  = "tcp"
  description                  = "Lustre ${each.key} back to benchmark nodes"
}

resource "aws_fsx_lustre_file_system" "this" {
  count = var.enable_fsx_lustre ? 1 : 0

  storage_capacity   = var.fsx_storage_capacity_gb
  subnet_ids         = [aws_subnet.public[0].id]
  security_group_ids = [aws_security_group.fsx[0].id]
  deployment_type    = "SCRATCH_2"
  # Data repository associations require Lustre 2.12 or newer; the provider
  # default for SCRATCH_2 is older, which would fail the association below.
  file_system_type_version = "2.15"

  tags = { Name = "${var.name_prefix}" }
}

# Links the filesystem to the corpus bucket. Files are imported lazily on
# first access, so the first read of a file pays an S3 fetch and later reads
# are served from Lustre. The harness measures both states separately -- the
# cold number is what a fresh GPU node actually experiences, and reporting
# only the warm number would flatter Lustre badly.
resource "aws_fsx_data_repository_association" "corpus" {
  count = var.enable_fsx_lustre ? 1 : 0

  file_system_id       = aws_fsx_lustre_file_system.this[0].id
  data_repository_path = "s3://${data.terraform_remote_state.data.outputs.bucket}"
  file_system_path     = "/corpus"

  s3 {
    auto_import_policy {
      events = ["NEW", "CHANGED", "DELETED"]
    }
  }
}
