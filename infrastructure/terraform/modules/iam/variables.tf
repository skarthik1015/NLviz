variable "project" {
  type        = string
  description = "Project name prefix"
}

variable "environment" {
  type        = string
  description = "Deployment environment"
}

variable "aws_region" {
  type        = string
  description = "AWS region"
}

variable "rds_secret_arn" {
  type        = string
  description = "ARN of the Secrets Manager secret containing RDS credentials"
}

variable "anthropic_secret_arn" {
  type        = string
  description = "ARN of the Secrets Manager secret containing the Anthropic API key"
}

variable "openai_secret_arn" {
  type = string
  description = "ARN of the Secrets Manager secret containing the OpenAI API key"
}

variable "uploads_bucket_arn" {
  type        = string
  description = "ARN of the S3 bucket for uploaded datasets"
}

variable "schemas_bucket_arn" {
  type        = string
  description = "ARN of the S3 bucket for generated schema YAML files"
}

variable "github_org" {
  type        = string
  description = "GitHub organisation or user that owns the repository (for OIDC trust)"
}

variable "github_repo" {
  type        = string
  description = "GitHub repository name (for OIDC trust policy)"
}

variable "ecr_backend_arn" {
  type        = string
  description = "ARN of the backend ECR repository"
}

variable "ecr_frontend_arn" {
  type        = string
  description = "ARN of the frontend ECR repository"
}
