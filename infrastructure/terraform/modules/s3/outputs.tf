output "uploads_bucket_name" {
  description = "Name of the S3 bucket for user uploads"
  value       = aws_s3_bucket.uploads.bucket
}

output "uploads_bucket_arn" {
  description = "ARN of the S3 bucket for user uploads"
  value       = aws_s3_bucket.uploads.arn
}

output "schemas_bucket_name" {
  description = "Name of the S3 bucket for generated schema YAML files"
  value       = aws_s3_bucket.schemas.bucket
}

output "schemas_bucket_arn" {
  description = "ARN of the S3 bucket for generated schema YAML files"
  value       = aws_s3_bucket.schemas.arn
}
