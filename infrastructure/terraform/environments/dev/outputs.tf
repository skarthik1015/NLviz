output "alb_dns_name" {
  description = "Public URL of the app (set as NEXT_PUBLIC_API_BASE_URL host in CI and ALB_DNS_NAME GitHub secret)"
  value       = module.alb.alb_dns_name
}

output "app_url" {
  description = "Full URL of the frontend"
  value       = "https://${var.domain_name}"
}

output "api_base_url" {
  description = "Full API base URL (use as NEXT_PUBLIC_API_BASE_URL in frontend build)"
  value       = "https://${var.domain_name}/api"
}

output "ecr_backend_url" {
  description = "ECR repository URL for backend images — set as ECR_BACKEND_URL GitHub secret"
  value       = module.ecr.backend_repository_url
}

output "ecr_frontend_url" {
  description = "ECR repository URL for frontend images — set as ECR_FRONTEND_URL GitHub secret"
  value       = module.ecr.frontend_repository_url
}

output "ecr_registry_id" {
  description = "ECR registry ID (AWS account ID)"
  value       = module.ecr.registry_id
}

output "ecs_cluster_name" {
  description = "ECS cluster name — used in CI/CD aws ecs update-service commands"
  value       = module.ecs.cluster_name
}

output "backend_service_name" {
  description = "ECS backend service name"
  value       = module.ecs.backend_service_name
}

output "frontend_service_name" {
  description = "ECS frontend service name"
  value       = module.ecs.frontend_service_name
}

output "rds_endpoint" {
  description = "RDS instance endpoint (host:port)"
  value       = module.rds.db_instance_endpoint
  sensitive   = true
}

output "github_deploy_role_arn" {
  description = "IAM role ARN for GitHub Actions OIDC — set as AWS_ROLE_ARN GitHub secret"
  value       = module.iam.github_deploy_role_arn
}

output "uploads_bucket_name" {
  description = "S3 bucket name for uploads"
  value       = module.s3.uploads_bucket_name
}

output "schemas_bucket_name" {
  description = "S3 bucket name for schemas"
  value       = module.s3.schemas_bucket_name
}

output "post_deploy_instructions" {
  description = "Steps required after first terraform apply"
  value       = <<-EOT
    ── Post-deploy steps ──────────────────────────────────────────────
    1. Before apply, register/delegate your domain and set `domain_name` in `terraform.tfvars`.

    2. Run `terraform apply` to create/update DNS, Cognito, ALB HTTPS, and related infrastructure.

    3. Store Anthropic API key:
       aws secretsmanager put-secret-value \
         --secret-id ${module.secrets.anthropic_secret_name} \
         --secret-string '{"api_key":"YOUR_SK_ANT_KEY"}'

    4. Add GitHub repository secrets:
       AWS_ROLE_ARN          = ${module.iam.github_deploy_role_arn}
       AWS_REGION            = ${var.aws_region}
       ECR_BACKEND_URL       = ${module.ecr.backend_repository_url}
       ECR_FRONTEND_URL      = ${module.ecr.frontend_repository_url}
       ECS_CLUSTER_NAME      = ${module.ecs.cluster_name}
       ECS_BACKEND_SERVICE   = ${module.ecs.backend_service_name}
       ECS_FRONTEND_SERVICE  = ${module.ecs.frontend_service_name}
       ALB_DNS_NAME          = ${module.alb.alb_dns_name}
       API_BASE_URL          = https://${var.domain_name}/api

    5. For local development, set DEV_USER_ID=<your-id> to bypass ALB/Cognito auth.
    6. Push to main → CI/CD deploys automatically.
    7. Visit: https://${var.domain_name}
    ───────────────────────────────────────────────────────────────────
  EOT
}
