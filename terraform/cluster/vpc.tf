locals {
  azs      = slice(data.aws_availability_zones.available.names, 0, 2)
  vpc_cidr = "10.42.0.0/16"
}

resource "aws_vpc" "this" {
  cidr_block           = local.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = var.name_prefix }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = var.name_prefix }
}

# Public subnets with auto-assigned public IPs, and deliberately NO NAT gateway.
#
# A NAT gateway would sit directly in the S3 read path, add a per-GB charge on
# every byte of every benchmark run, and impose its own bandwidth behaviour on
# the thing being measured. Public subnets plus the S3 gateway endpoint below
# keep S3 traffic on a private, free, unmetered path. The node's security group
# allows no inbound traffic, so the public IP is only used for egress
# (ECR image pulls, package installs).
resource "aws_subnet" "public" {
  count                   = length(local.azs)
  vpc_id                  = aws_vpc.this.id
  cidr_block              = cidrsubnet(local.vpc_cidr, 4, count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true

  tags = {
    Name                     = "${var.name_prefix}-public-${local.azs[count.index]}"
    "kubernetes.io/role/elb" = "1"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "${var.name_prefix}-public" }
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# Gateway endpoint: S3 traffic never leaves the AWS network, is not metered,
# and is not subject to the internet gateway's path. This matches how a
# production GPU node should be configured, and removes a variable that would
# otherwise contaminate every number in the report.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id]
  tags              = { Name = "${var.name_prefix}-s3" }
}
