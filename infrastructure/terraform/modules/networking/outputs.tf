output "vpc_id" {
  description = "ID of the VPC"
  value       = aws_vpc.main.id
}

output "public_subnet_ids" {
  description = "IDs of public subnets (ALB lives here)"
  value       = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  description = "IDs of private subnets (ECS tasks and RDS live here)"
  value       = aws_subnet.private[*].id
}

output "alb_sg_id" {
  description = "Security group ID for the ALB"
  value       = aws_security_group.alb.id
}

output "backend_sg_id" {
  description = "Security group ID for backend ECS tasks"
  value       = aws_security_group.backend.id
}

output "frontend_sg_id" {
  description = "Security group ID for frontend ECS tasks"
  value       = aws_security_group.frontend.id
}

output "rds_sg_id" {
  description = "Security group ID for RDS"
  value       = aws_security_group.rds.id
}
