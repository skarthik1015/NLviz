terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

# ── Variables ────────────────────────────────────────────────────────

variable "aws_region" {
  type        = string
  description = "AWS region to deploy resources into"
  default     = "us-east-1"
}

variable "project" {
  type        = string
  description = "Project name used as a prefix for all resource names"
  default     = "nl-query-tool"
}

# ── S3 Bucket for Terraform State ────────────────────────────────────

resource "aws_s3_bucket" "terraform_state" {
  bucket = "${var.project}-terraform-state-${data.aws_caller_identity.current.account_id}"

  tags = {
    Project     = var.project
    ManagedBy   = "terraform"
    Purpose     = "terraform-state"
  }
}

resource "aws_s3_bucket_versioning" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "terraform_state" {
  bucket                  = aws_s3_bucket.terraform_state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ── DynamoDB Table for State Locking ─────────────────────────────────

resource "aws_dynamodb_table" "terraform_locks" {
  name         = "${var.project}-terraform-locks"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  tags = {
    Project   = var.project
    ManagedBy = "terraform"
    Purpose   = "terraform-state-lock"
  }
}

# ── Outputs ───────────────────────────────────────────────────────────

output "state_bucket_name" {
  description = "S3 bucket name for Terraform state"
  value       = aws_s3_bucket.terraform_state.bucket
}

output "lock_table_name" {
  description = "DynamoDB table name for Terraform state locking"
  value       = aws_dynamodb_table.terraform_locks.name
}

output "backend_config" {
  description = "Copy this backend block into each environment's terraform block"
  value       = <<-EOT
    terraform {
      backend "s3" {
        bucket         = "${aws_s3_bucket.terraform_state.bucket}"
        key            = "environments/dev/terraform.tfstate"
        region         = "${var.aws_region}"
        dynamodb_table = "${aws_dynamodb_table.terraform_locks.name}"
        encrypt        = true
      }
    }
  EOT
}
