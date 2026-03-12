output "backend_log_group_name" {
  description = "CloudWatch log group name for backend ECS tasks"
  value       = aws_cloudwatch_log_group.backend.name
}

output "frontend_log_group_name" {
  description = "CloudWatch log group name for frontend ECS tasks"
  value       = aws_cloudwatch_log_group.frontend.name
}
