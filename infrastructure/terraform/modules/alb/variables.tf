variable "project" {
  type        = string
  description = "Project name prefix"
}

variable "environment" {
  type        = string
  description = "Deployment environment"
}

variable "vpc_id" {
  type        = string
  description = "VPC ID"
}

variable "public_subnet_ids" {
  type        = list(string)
  description = "Public subnet IDs — ALB is placed here"
}

variable "alb_sg_id" {
  type        = string
  description = "Security group ID for the ALB"
}

# ── Cognito + HTTPS ──────────────────────────────────────────────────

variable "certificate_arn" {
  type        = string
  description = "ARN of the ACM certificate for HTTPS"
  default     = null
  nullable    = true
}

variable "cognito_user_pool_arn" {
  type        = string
  description = "ARN of the Cognito user pool"
  default     = null
  nullable    = true
}

variable "cognito_user_pool_client_id" {
  type        = string
  description = "Cognito app client ID for ALB integration"
  default     = null
  nullable    = true
}

variable "cognito_user_pool_domain" {
  type        = string
  description = "Cognito hosted UI domain prefix"
  default     = null
  nullable    = true
}
