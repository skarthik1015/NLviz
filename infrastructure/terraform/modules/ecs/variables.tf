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

variable "private_subnet_ids" {
  type        = list(string)
  description = "Private subnet IDs for ECS tasks"
}

variable "backend_sg_id" {
  type        = string
  description = "Security group ID for backend ECS tasks"
}

variable "frontend_sg_id" {
  type        = string
  description = "Security group ID for frontend ECS tasks"
}

variable "backend_target_group_arn" {
  type        = string
  description = "ALB target group ARN for backend service"
}

variable "frontend_target_group_arn" {
  type        = string
  description = "ALB target group ARN for frontend service"
}

variable "task_execution_role_arn" {
  type        = string
  description = "ARN of the ECS task execution role"
}

variable "task_role_arn" {
  type        = string
  description = "ARN of the ECS task role"
}

variable "backend_image" {
  type        = string
  description = "Full ECR image URI for the backend (e.g. 123456789.dkr.ecr.us-east-1.amazonaws.com/project/backend:latest)"
}

variable "frontend_image" {
  type        = string
  description = "Full ECR image URI for the frontend"
}

variable "backend_log_group" {
  type        = string
  description = "CloudWatch log group name for backend"
}

variable "frontend_log_group" {
  type        = string
  description = "CloudWatch log group name for frontend"
}

variable "rds_secret_arn" {
  type        = string
  description = "Secrets Manager ARN for RDS credentials (injected as DATABASE_URL)"
}

variable "openai_secret_arn" {
  type        = string
  description = "Secrets Manager ARN for OpenAI API key"
}

variable "anthropic_secret_arn" {
  type        = string
  description = "Secrets Manager ARN for Anthropic API key"
  default     = null
}

variable "uploads_bucket_name" {
  type        = string
  description = "S3 bucket name for user uploads"
}

variable "schemas_bucket_name" {
  type        = string
  description = "S3 bucket name for generated schema YAML files"
}

variable "alb_dns_name" {
  type        = string
  description = "ALB DNS name used in CORS and as API base URL"
}

variable "backend_dev_user_id" {
  type        = string
  description = "Optional dev-only fallback user id for hosted stacks without ALB/Cognito auth"
  default     = null
  nullable    = true
}

variable "backend_cpu" {
  type        = number
  description = "Backend task CPU units (1024 = 1 vCPU)"
  default     = 1024
}

variable "backend_memory_mb" {
  type        = number
  description = "Backend task memory in MB"
  default     = 2048
}

variable "frontend_cpu" {
  type        = number
  description = "Frontend task CPU units"
  default     = 512
}

variable "frontend_memory_mb" {
  type        = number
  description = "Frontend task memory in MB"
  default     = 1024
}

variable "backend_desired_count" {
  type        = number
  description = "Desired number of backend task instances"
  default     = 1
}

variable "frontend_desired_count" {
  type        = number
  description = "Desired number of frontend task instances"
  default     = 1
}
