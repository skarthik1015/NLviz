variable "project" {
  type        = string
  description = "Project name prefix"
}

variable "environment" {
  type        = string
  description = "Deployment environment"
}

variable "uploads_expiry_days" {
  type        = number
  description = "Days after which uploaded raw files expire (cost control)"
  default     = 90
}

variable "alb_dns_name" {
  type        = string
  description = "ALB DNS name — added to S3 CORS allowed origins"
}
