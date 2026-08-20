variable "region" {
  description = "AWS region. Must match the region the benchmark cluster runs in; cross-region S3 reads would measure the internet, not the filesystem."
  type        = string
  default     = "us-west-2"
}

variable "aws_profile" {
  description = "Local AWS CLI profile used by Terraform."
  type        = string
  default     = "agent-toolkit"
}

variable "name_prefix" {
  description = "Prefix for all resource names."
  type        = string
  default     = "s3fuse-bench"
}

variable "corpus_expiration_days" {
  description = "Delete corpus objects after this many days so a forgotten bucket cannot accrue cost forever. Set to 0 to disable expiration."
  type        = number
  default     = 30
}
