output "alb_sg_security_group_arn" {
  value = module.alb_sg.security_group_arn
}
output "alb_sg_security_group_id" {
  value = module.alb_sg.security_group_id
}
output "alb_sg_security_group_name" {
  value = module.alb_sg.security_group_name
}
output "backend_sg_security_group_arn" {
  value = module.backend_sg.security_group_arn
}
output "backend_sg_security_group_id" {
  value = module.backend_sg.security_group_id
}
output "backend_sg_security_group_name" {
  value = module.backend_sg.security_group_name
}
output "cluster_arn" {
  value = module.cluster.arn
}
output "cluster_id" {
  value = module.cluster.id
}
output "cluster_name" {
  value = module.cluster.name
}
output "ecr_backend_registry_id" {
  value = module.ecr_backend.registry_id
}
output "ecr_backend_repository_arn" {
  value = module.ecr_backend.repository_arn
}
output "ecr_backend_repository_name" {
  value = module.ecr_backend.repository_name
}
output "ecr_backend_repository_url" {
  value = module.ecr_backend.repository_url
}
output "ecr_frontend_registry_id" {
  value = module.ecr_frontend.registry_id
}
output "ecr_frontend_repository_arn" {
  value = module.ecr_frontend.repository_arn
}
output "ecr_frontend_repository_name" {
  value = module.ecr_frontend.repository_name
}
output "ecr_frontend_repository_url" {
  value = module.ecr_frontend.repository_url
}
output "frontend_sg_security_group_arn" {
  value = module.frontend_sg.security_group_arn
}
output "frontend_sg_security_group_id" {
  value = module.frontend_sg.security_group_id
}
output "frontend_sg_security_group_name" {
  value = module.frontend_sg.security_group_name
}
output "instance_1_address" {
  value = module.instance_1.address
}
output "instance_1_arn" {
  value = module.instance_1.arn
}
output "instance_1_endpoint" {
  value = module.instance_1.endpoint
}
output "instance_1_id" {
  value = module.instance_1.id
}
output "instance_1_port" {
  value = module.instance_1.port
}
output "listener_arn" {
  value = module.listener.arn
}
output "listener_id" {
  value = module.listener.id
}
output "listener_rule_1_arn" {
  value = module.listener_rule_1.arn
}
output "listener_rule_1_id" {
  value = module.listener_rule_1.id
}
output "listener_rule_arn" {
  value = module.listener_rule.arn
}
output "listener_rule_id" {
  value = module.listener_rule.id
}
output "load_balancer_arn" {
  value = module.load_balancer.arn
}
output "load_balancer_dns_name" {
  value = module.load_balancer.dns_name
}
output "load_balancer_id" {
  value = module.load_balancer.id
}
output "load_balancer_zone_id" {
  value = module.load_balancer.zone_id
}
output "log_backend_log_group_arn" {
  value = module.log_backend.log_group_arn
}
output "log_backend_log_group_name" {
  value = module.log_backend.log_group_name
}
output "log_frontend_log_group_arn" {
  value = module.log_frontend.log_group_arn
}
output "log_frontend_log_group_name" {
  value = module.log_frontend.log_group_name
}
output "policy_1_policy_arn" {
  value = module.policy_1.policy_arn
}
output "policy_2_policy_arn" {
  value = module.policy_2.policy_arn
}
output "policy_3_policy_arn" {
  value = module.policy_3.policy_arn
}
output "policy_5_policy_arn" {
  value = module.policy_5.policy_arn
}
output "policy_attachment_1_attachment_id" {
  value = module.policy_attachment_1.attachment_id
}
output "policy_attachment_2_attachment_id" {
  value = module.policy_attachment_2.attachment_id
}
output "policy_attachment_3_attachment_id" {
  value = module.policy_attachment_3.attachment_id
}
output "policy_attachment_4_attachment_id" {
  value = module.policy_attachment_4.attachment_id
}
output "policy_attachment_6_attachment_id" {
  value = module.policy_attachment_6.attachment_id
}
output "policy_attachment_attachment_id" {
  value = module.policy_attachment.attachment_id
}
output "policy_policy_arn" {
  value = module.policy.policy_arn
}
output "rds_sg_security_group_arn" {
  value = module.rds_sg.security_group_arn
}
output "rds_sg_security_group_id" {
  value = module.rds_sg.security_group_id
}
output "rds_sg_security_group_name" {
  value = module.rds_sg.security_group_name
}
output "role_1_role_arn" {
  value = module.role_1.role_arn
}
output "role_1_role_name" {
  value = module.role_1.role_name
}
output "role_3_role_arn" {
  value = module.role_3.role_arn
}
output "role_3_role_name" {
  value = module.role_3.role_name
}
output "role_role_arn" {
  value = module.role.role_arn
}
output "role_role_name" {
  value = module.role.role_name
}
output "s3_schemas_arn" {
  value = module.s3_schemas.arn
}
output "s3_schemas_bucket_domain_name" {
  value = module.s3_schemas.bucket_domain_name
}
output "s3_schemas_bucket_regional_domain_name" {
  value = module.s3_schemas.bucket_regional_domain_name
}
output "s3_schemas_id" {
  value = module.s3_schemas.id
}
output "s3_uploads_arn" {
  value = module.s3_uploads.arn
}
output "s3_uploads_bucket_domain_name" {
  value = module.s3_uploads.bucket_domain_name
}
output "s3_uploads_bucket_regional_domain_name" {
  value = module.s3_uploads.bucket_regional_domain_name
}
output "s3_uploads_id" {
  value = module.s3_uploads.id
}
output "secret_1_arn" {
  value = module.secret_1.arn
}
output "secret_1_id" {
  value = module.secret_1.id
}
output "secret_1_name" {
  value = module.secret_1.name
}
output "secret_anthropic_arn" {
  value = module.secret_anthropic.arn
}
output "secret_anthropic_id" {
  value = module.secret_anthropic.id
}
output "secret_anthropic_name" {
  value = module.secret_anthropic.name
}
output "secret_openai_arn" {
  value = module.secret_openai.arn
}
output "secret_openai_id" {
  value = module.secret_openai.id
}
output "secret_openai_name" {
  value = module.secret_openai.name
}
output "service_1_cluster" {
  value = module.service_1.cluster
}
output "service_1_id" {
  value = module.service_1.id
}
output "service_1_name" {
  value = module.service_1.name
}
output "service_cluster" {
  value = module.service.cluster
}
output "service_id" {
  value = module.service.id
}
output "service_name" {
  value = module.service.name
}
output "subnet_group_1_arn" {
  value = module.subnet_group_1.arn
}
output "subnet_group_1_id" {
  value = module.subnet_group_1.id
}
output "subnet_group_1_name" {
  value = module.subnet_group_1.name
}
output "target_group_1_arn" {
  value = module.target_group_1.arn
}
output "target_group_1_id" {
  value = module.target_group_1.id
}
output "target_group_1_name" {
  value = module.target_group_1.name
}
output "target_group_arn" {
  value = module.target_group.arn
}
output "target_group_id" {
  value = module.target_group.id
}
output "target_group_name" {
  value = module.target_group.name
}
output "task_definition_1_arn" {
  value = module.task_definition_1.arn
}
output "task_definition_1_family" {
  value = module.task_definition_1.family
}
output "task_definition_1_revision" {
  value = module.task_definition_1.revision
}
output "task_definition_arn" {
  value = module.task_definition.arn
}
output "task_definition_family" {
  value = module.task_definition.family
}
output "task_definition_revision" {
  value = module.task_definition.revision
}
output "vpc_cidr_block" {
  value = module.vpc.cidr_block
}
output "vpc_id" {
  value = module.vpc.id
}
output "vpc_internet_gateway_id" {
  value = module.vpc.internet_gateway_id
}
output "vpc_nat_gateway_id" {
  value = module.vpc.nat_gateway_id
}
output "vpc_private_subnet_ids" {
  value = module.vpc.private_subnet_ids
}
output "vpc_public_subnet_ids" {
  value = module.vpc.public_subnet_ids
}
