# The test-data bucket lives in its own Terraform state so that
# `terraform destroy` on the cluster does not delete a corpus that
# takes many minutes and real PUT charges to regenerate.

resource "random_id" "suffix" {
  byte_length = 4
}

resource "aws_s3_bucket" "corpus" {
  bucket        = "${var.name_prefix}-corpus-${random_id.suffix.hex}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "corpus" {
  bucket                  = aws_s3_bucket.corpus.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "corpus" {
  bucket = aws_s3_bucket.corpus.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    # SSE-S3 rather than SSE-KMS on purpose: per-request KMS decryption adds
    # latency and a throttling ceiling that would show up as a filesystem
    # difference when it is really a KMS difference.
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "corpus" {
  bucket = aws_s3_bucket.corpus.id

  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  dynamic "rule" {
    for_each = var.corpus_expiration_days > 0 ? [1] : []
    content {
      id     = "expire-corpus"
      status = "Enabled"
      filter {}
      expiration {
        days = var.corpus_expiration_days
      }
    }
  }
}
