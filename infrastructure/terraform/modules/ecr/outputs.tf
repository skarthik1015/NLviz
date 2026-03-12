output "backend_repository_url" {
  description = "ECR URL for backend image (used in task definition and CI/CD)"
  value       = aws_ecr_repository.backend.repository_url
}

output "frontend_repository_url" {
  description = "ECR URL for frontend image (used in task definition and CI/CD)"
  value       = aws_ecr_repository.frontend.repository_url
}

output "registry_id" {
  description = "ECR registry ID (AWS account ID)"
  value       = aws_ecr_repository.backend.registry_id
}

output "backend_repository_arn" {
  description = "ARN of backend ECR repo"
  value = aws_ecr_repository.backend.arn
}

output "frontend_repository_arn" {
  description = "ARN of frontend ECR repo"
  value = aws_ecr_repository.frontend.arn
}
