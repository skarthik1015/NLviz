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
  default     = "dev.nlqtool.com"
  description = "Root domain name (e.g. nlquerytool.com)"
}

variable "alb_dns_name" {
  type        = string
  description = "DNS name of the ALB to create an alias record for"
}

variable "alb_zone_id" {
  type        = string
  description = "Route 53 hosted zone ID of the ALB (for alias target)"
}
