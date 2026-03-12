variable "project" {
  type        = string
  description = "Project name prefix for all resource names"
}

variable "environment" {
  type        = string
  description = "Deployment environment (e.g. dev, prod)"
}

variable "vpc_cidr" {
  type        = string
  description = "CIDR block for the VPC"
  default     = "10.0.0.0/16"
}
