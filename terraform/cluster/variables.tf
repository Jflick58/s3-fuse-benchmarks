variable "region" {
  type    = string
  default = "us-west-2"
}

variable "aws_profile" {
  type    = string
  default = "agent-toolkit"
}

variable "name_prefix" {
  type    = string
  default = "s3fuse-bench"
}

variable "kubernetes_version" {
  description = "EKS control plane version."
  type        = string
  default     = "1.34"
}

variable "node_instance_type" {
  description = <<-DESC
    Instance type for the single benchmark node.

    This is the most consequential variable in the harness: for large
    sequential reads the network pipe is usually the binding constraint, so
    the instance type decides whether the fast clients separate from each
    other or all pin at the same ceiling.

    The default is the cheapest type that has BOTH instance-store NVMe (needed
    by the local-prefetch baseline) and a 25 Gbps ceiling. No GPU: the
    benchmark measures S3-to-node transfer, and a GPU would add cost without
    changing a single I/O number.

    Note "Up to N Gigabit" means burstable. Burst credits deplete partway
    through a long read, which quietly turns a throughput benchmark into a
    measurement of burst-credit accounting. If the report flags burst
    exhaustion, step up to a sustained-bandwidth type before trusting the
    ranking of the fastest clients.

      c6id.2xlarge    8 vCPU  up to 12.5 Gbps (burst)   474 GB NVMe  cheapest
      m5dn.2xlarge    8 vCPU  up to 25 Gbps   (burst)   300 GB NVMe  <- default
      m5dn.4xlarge   16 vCPU  up to 25 Gbps   (burst)   600 GB NVMe
      m5dn.8xlarge   32 vCPU  25 Gbps SUSTAINED        1200 GB NVMe  needs quota
  DESC
  type        = string
  default     = "m5dn.2xlarge"
}

variable "node_root_volume_gb" {
  description = "Root EBS volume. Only holds the OS and container images; the benchmark uses instance-store NVMe."
  type        = number
  default     = 100
}

variable "enable_fsx_lustre" {
  description = "Create an FSx for Lustre filesystem linked to the corpus bucket. Adds roughly USD 0.23/hr for the default 1200 GiB scratch filesystem and about 10 minutes to apply time."
  type        = bool
  default     = false
}

variable "fsx_storage_capacity_gb" {
  description = "FSx for Lustre capacity in GiB. SCRATCH_2 minimum is 1200."
  type        = number
  default     = 1200
}

variable "allow_ssh_from_cidr" {
  description = "Optional CIDR permitted to reach the node on port 22 for manual debugging. Empty means no inbound access at all; use `kubectl exec` instead."
  type        = string
  default     = ""
}

variable "node_sysctl_tuning" {
  description = <<-DESC
    Apply high-throughput TCP tuning (larger socket buffers) to the node.

    Defaults to false on purpose. Your production GPU nodes almost certainly
    run stock kernel defaults, so tuning here would make the benchmark
    describe a machine you do not actually operate. Turn it on only to answer
    the separate question "would host tuning help?", and compare against a
    baseline run with it off. Either way the setting is recorded in the run
    metadata so results stay interpretable.
  DESC
  type        = bool
  default     = false
}
