locals {
  tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

resource "aws_cloudwatch_log_group" "backend" {
  name              = "/ecs/${var.project}/${var.environment}/backend"
  retention_in_days = var.backend_log_retention_days
  tags              = merge(local.tags, { Service = "backend" })
}

resource "aws_cloudwatch_log_group" "frontend" {
  name              = "/ecs/${var.project}/${var.environment}/frontend"
  retention_in_days = var.frontend_log_retention_days
  tags              = merge(local.tags, { Service = "frontend" })
}
