data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

# ── Uploads Bucket ────────────────────────────────────────────────────
# Stores raw CSV / Parquet files uploaded by users.
# DuckDB httpfs reads these directly — no download to ECS disk needed.

resource "aws_s3_bucket" "uploads" {
  bucket = "${var.project}-${var.environment}-uploads-${local.account_id}"
  tags   = merge(local.tags, { Purpose = "user-uploads" })

  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_versioning" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "aws:kms" }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "uploads" {
  bucket                  = aws_s3_bucket.uploads.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  rule {
    id     = "expire-raw-uploads"
    status = "Enabled"
    expiration { days = var.uploads_expiry_days }
    noncurrent_version_expiration { noncurrent_days = 30 }
  }
}

resource "aws_s3_bucket_cors_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["PUT", "POST", "GET"]
    allowed_origins = ["http://${var.alb_dns_name}"]
    expose_headers  = ["ETag"]
    max_age_seconds = 3600
  }
}

# ── Schemas Bucket ────────────────────────────────────────────────────
# Stores auto-generated semantic YAML schema files.
# Small files, kept indefinitely, shared across container restarts.

resource "aws_s3_bucket" "schemas" {
  bucket = "${var.project}-${var.environment}-schemas-${local.account_id}"
  tags   = merge(local.tags, { Purpose = "generated-schemas" })

  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_versioning" "schemas" {
  bucket = aws_s3_bucket.schemas.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "schemas" {
  bucket = aws_s3_bucket.schemas.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "aws:kms" }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "schemas" {
  bucket                  = aws_s3_bucket.schemas.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
