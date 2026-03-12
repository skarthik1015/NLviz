output "anthropic_secret_arn" {
  description = "ARN of the Anthropic API key secret"
  value       = aws_secretsmanager_secret.anthropic_api_key.arn
}

output "anthropic_secret_name" {
  description = "Name of the Anthropic API key secret"
  value       = aws_secretsmanager_secret.anthropic_api_key.name
}
