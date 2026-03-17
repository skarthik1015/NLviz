output "user_pool_id" {
  description = "Cognito user pool ID"
  value       = aws_cognito_user_pool.main.id
}

output "user_pool_arn" {
  description = "Cognito user pool ARN"
  value       = aws_cognito_user_pool.main.arn
}

output "user_pool_client_id" {
  description = "App client ID for ALB integration"
  value       = aws_cognito_user_pool_client.alb.id
}

output "user_pool_client_secret" {
  description = "App client secret for ALB integration"
  value       = aws_cognito_user_pool_client.alb.client_secret
  sensitive   = true
}

output "user_pool_domain" {
  description = "Cognito hosted UI domain (prefix only)"
  value       = aws_cognito_user_pool_domain.main.domain
}

output "user_pool_endpoint" {
  description = "Cognito user pool endpoint (issuer URL)"
  value       = aws_cognito_user_pool.main.endpoint
}
