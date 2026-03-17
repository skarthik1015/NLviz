variable "project" {
  type        = string
  description = "Project name prefix for all resources"
  default     = "nl-query-tool"
}

variable "environment" {
  type        = string
  description = "Deployment environment name"
  default     = "dev"
}

variable "aws_region" {
  type        = string
  description = "AWS region"
  default     = "us-east-1"
}

variable "github_org" {
  type        = string
  description = "GitHub organisation or user that owns the repository"
}

variable "github_repo" {
  type        = string
  description = "GitHub repository name"
  default     = "nl-query-tool"
}

variable "backend_image_tag" {
  type        = string
  description = "Docker image tag for the backend (updated by CI/CD)"
  default     = "latest"
}

variable "frontend_image_tag" {
  type        = string
  description = "Docker image tag for the frontend (updated by CI/CD)"
  default     = "latest"
}

variable "domain_name" {
  type        = string
  description = "Custom domain name for the application (e.g. nlquerytool.com)"
  default     = null
  nullable    = true
}

variable "cognito_domain_prefix" {
  type        = string
  description = "Globally unique prefix for the Cognito hosted UI domain"
  default     = null
  nullable    = true
}

variable "backend_dev_user_id" {
  type        = string
  description = "Dev-only fallback owner id injected into the hosted backend when ALB/Cognito auth is disabled"
  default     = "karthik"
}
