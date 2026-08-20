output "bucket" {
  description = "Name of the corpus bucket."
  value       = aws_s3_bucket.corpus.id
}

output "bucket_arn" {
  description = "ARN of the corpus bucket."
  value       = aws_s3_bucket.corpus.arn
}

output "region" {
  description = "Region the corpus bucket lives in."
  value       = var.region
}
