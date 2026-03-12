variable "project" {
  type        = string
  description = "Project name prefix"
}

variable "environment" {
  type        = string
  description = "Deployment environment"
}

variable "image_retention_count" {
  type        = number
  description = "Number of images to retain in each repository"
  default     = 10
}
