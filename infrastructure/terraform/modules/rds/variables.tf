variable "project" {
  type        = string
  description = "Project name prefix"
}

variable "environment" {
  type        = string
  description = "Deployment environment"
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "IDs of private subnets for the RDS subnet group"
}

variable "rds_sg_id" {
  type        = string
  description = "Security group ID to attach to the RDS instance"
}

variable "instance_class" {
  type        = string
  description = "RDS instance class"
  default     = "db.t3.small"
}

variable "allocated_storage_gb" {
  type        = number
  description = "Initial allocated storage in GB"
  default     = 20
}

variable "db_name" {
  type        = string
  description = "Name of the initial database to create"
  default     = "nlquerytool"
}

variable "db_username" {
  type        = string
  description = "Master username for the RDS instance"
  default     = "appuser"
}
