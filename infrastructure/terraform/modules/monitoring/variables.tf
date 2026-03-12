variable "project" {
  type        = string
  description = "Project name prefix"
}

variable "environment" {
  type        = string
  description = "Deployment environment"
}

variable "backend_log_retention_days" {
  type        = number
  description = "CloudWatch log retention for backend logs"
  default     = 30
}

variable "frontend_log_retention_days" {
  type        = number
  description = "CloudWatch log retention for frontend logs"
  default     = 7
}
