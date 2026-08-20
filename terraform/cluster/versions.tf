terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
    tls = { source = "hashicorp/tls", version = "~> 4.0" }
  }
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile
  default_tags {
    tags = {
      Project   = "s3-fuse-benchmarks"
      ManagedBy = "terraform"
      Component = "cluster"
    }
  }
}

# The corpus bucket is managed in a separate state so `terraform destroy`
# here never touches the test data.
data "terraform_remote_state" "data" {
  backend = "local"
  config = {
    path = "${path.module}/../data/terraform.tfstate"
  }
}

data "aws_availability_zones" "available" {
  state = "available"
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

data "aws_caller_identity" "current" {}
