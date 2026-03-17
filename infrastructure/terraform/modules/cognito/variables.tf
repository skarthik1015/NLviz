variable "project" {
  type        = string
  description = "Project name prefix"
}

variable "environment" {
  type        = string
  description = "Deployment environment"
}

variable "domain_name" {
  type        = string
  description = "Application domain name — used for callback/logout URLs"
}

# data "domain_name" "main" {
#   domain_name = var.domain_name
# }

variable "cognito_domain_prefix" {
  type        = string
  description = "Cognito hosted UI domain prefix (must be globally unique)"
}
