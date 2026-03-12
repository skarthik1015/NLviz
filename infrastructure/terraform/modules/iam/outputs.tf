output "task_execution_role_arn" {
  description = "ARN of the ECS task execution role"
  value       = aws_iam_role.ecs_task_execution.arn
}

output "task_role_arn" {
  description = "ARN of the ECS task role (permissions available inside the container)"
  value       = aws_iam_role.ecs_task.arn
}

output "github_deploy_role_arn" {
  description = "ARN of the GitHub Actions OIDC deploy role — use as role-to-assume in CI/CD"
  value       = aws_iam_role.github_deploy.arn
}
