output "db_instance_endpoint" {
  description = "RDS instance endpoint (host:port)"
  value       = "${aws_db_instance.main.address}:${aws_db_instance.main.port}"
}

output "db_instance_address" {
  description = "RDS instance hostname"
  value       = aws_db_instance.main.address
}

output "db_secret_arn" {
  description = "ARN of the Secrets Manager secret containing RDS credentials and connection_string"
  value       = aws_secretsmanager_secret.rds_credentials.arn
}

output "db_secret_name" {
  description = "Name of the Secrets Manager secret containing RDS credentials"
  value       = aws_secretsmanager_secret.rds_credentials.name
}
