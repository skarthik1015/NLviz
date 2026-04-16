module "alb_sg" {
  source = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/ec2/security_group?ref=7786a6b944ff99c1f5e848f327f6512416c710aa"
  ingress_rules = [{
    cidr_blocks = ["0.0.0.0/0"]
    from_port   = 80
    protocol    = "tcp"
    to_port     = 80
    }, {
    cidr_blocks = ["0.0.0.0/0"]
    from_port   = 443
    protocol    = "tcp"
    to_port     = 443
  }]
  name   = "nl-query-tool-dev-alb"
  vpc_id = module.vpc.id
}
module "backend_sg" {
  source = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/ec2/security_group?ref=7786a6b944ff99c1f5e848f327f6512416c710aa"
  ingress_rules = [{
    from_port       = 8000
    protocol        = "tcp"
    security_groups = [module.alb_sg.security_group_id]
    to_port         = 8000
  }]
  name   = "nl-query-tool-dev-backend"
  vpc_id = module.vpc.id
}
module "cluster" {
  source = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/ecs/cluster?ref=7786a6b944ff99c1f5e848f327f6512416c710aa"
  name   = "nl-query-tool-dev"
}
module "ecr_backend" {
  source = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/ecr/repository?ref=7786a6b944ff99c1f5e848f327f6512416c710aa"
  name   = "nl-query-tool/backend"
  force_delete = true
}
module "ecr_frontend" {
  source = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/ecr/repository?ref=7786a6b944ff99c1f5e848f327f6512416c710aa"
  name   = "nl-query-tool/frontend"
  force_delete = true
}
module "frontend_sg" {
  source = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/ec2/security_group?ref=7786a6b944ff99c1f5e848f327f6512416c710aa"
  ingress_rules = [{
    from_port       = 3000
    protocol        = "tcp"
    security_groups = [module.alb_sg.security_group_id]
    to_port         = 3000
  }]
  name   = "nl-query-tool-dev-frontend"
  vpc_id = module.vpc.id
}
module "instance_1" {
  source                 = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/rds/instance?ref=9451c7cffba19f0d891dd19212e0e5c54b4d6ab0"
  db_name                = "nlquerytool"
  db_subnet_group_name   = module.subnet_group_1.name
  deletion_protection    = false
  name                   = "nl-query-tool-dev"
  password               = "CHANGE_BEFORE_APPLY"
  skip_final_snapshot    = true
  username               = "nlqueryadmin"
  vpc_security_group_ids = [module.rds_sg.security_group_id]
}
module "listener" {
  source                          = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/alb/listener?ref=7786a6b944ff99c1f5e848f327f6512416c710aa"
  default_action_target_group_arn = module.target_group_1.arn
  default_action_type             = "forward"
  load_balancer_arn               = module.load_balancer.arn
  port                            = 80
}
module "listener_rule" {
  source           = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/alb/listener_rule?ref=ff52f65e7bf0feda762574ae1609231a25c852f3"
  action_type      = "forward"
  listener_arn     = module.listener.arn
  name             = "healthz"
  path_patterns    = ["/healthz"]
  priority         = 1
  target_group_arn = module.target_group.arn
  depends_on       = [module.target_group]
}
module "listener_rule_1" {
  source           = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/alb/listener_rule?ref=ff52f65e7bf0feda762574ae1609231a25c852f3"
  action_type      = "forward"
  listener_arn     = module.listener.arn
  name             = "api"
  path_patterns    = ["/api/*"]
  priority         = 2
  target_group_arn = module.target_group.arn
  depends_on       = [module.target_group]
}
module "load_balancer" {
  source             = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/alb/load_balancer?ref=7786a6b944ff99c1f5e848f327f6512416c710aa"
  name               = "nl-query-tool-dev"
  security_group_ids = [module.alb_sg.security_group_id]
  subnet_ids         = module.vpc.public_subnet_ids
}
module "log_backend" {
  source = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/cloudwatch/log_group?ref=7786a6b944ff99c1f5e848f327f6512416c710aa"
  name   = "/ecs/nl-query-tool/dev/backend"
}
module "log_frontend" {
  source = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/cloudwatch/log_group?ref=7786a6b944ff99c1f5e848f327f6512416c710aa"
  name   = "/ecs/nl-query-tool/dev/frontend"
}
module "policy" {
  source          = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/iam/policy?ref=ff52f65e7bf0feda762574ae1609231a25c852f3"
  policy_document = jsonencode({"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"secretsmanager:GetSecretValue","Resource":[module.secret_1.arn,module.secret_openai.arn,module.secret_anthropic.arn]}]})
  policy_name     = "nl-query-tool-dev-read-startup-secrets"
}
module "policy_1" {
  source          = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/iam/policy?ref=ff52f65e7bf0feda762574ae1609231a25c852f3"
  policy_document = jsonencode({"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:ListBucket"],"Resource":[module.s3_uploads.arn,"${module.s3_uploads.arn}/*",module.s3_schemas.arn,"${module.s3_schemas.arn}/*"]}]})
  policy_name     = "nl-query-tool-dev-s3-uploads-schemas"
  tags = {
    policy = "s3"
  }
}
module "policy_2" {
  source          = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/iam/policy?ref=ff52f65e7bf0feda762574ae1609231a25c852f3"
  policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"secretsmanager:GetSecretValue\",\"secretsmanager:CreateSecret\",\"secretsmanager:PutSecretValue\",\"secretsmanager:DeleteSecret\",\"secretsmanager:DescribeSecret\"],\"Resource\":\"arn:aws:secretsmanager:*:*:secret:nl-query-tool/connections/*\"}]}"
  policy_name     = "nl-query-tool-dev-manage-connection-secrets"
  tags = {
    policy = "conn-secrets"
  }
}
module "policy_3" {
  source          = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/iam/policy?ref=ff52f65e7bf0feda762574ae1609231a25c852f3"
  policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"cloudwatch:PutMetricData\",\"Resource\":\"*\",\"Condition\":{\"StringEquals\":{\"cloudwatch:namespace\":\"NLQueryTool\"}}}]}"
  policy_name     = "nl-query-tool-dev-emit-custom-metrics"
  tags = {
    policy = "metrics"
  }
}
module "policy_5" {
  source          = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/iam/policy?ref=ff52f65e7bf0feda762574ae1609231a25c852f3"
  policy_document = jsonencode({"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"ecr:GetAuthorizationToken","Resource":"*"},{"Effect":"Allow","Action":["ecr:BatchCheckLayerAvailability","ecr:CompleteLayerUpload","ecr:InitiateLayerUpload","ecr:PutImage","ecr:UploadLayerPart","ecr:BatchGetImage","ecr:GetDownloadUrlForLayer"],"Resource":[module.ecr_backend.repository_arn,module.ecr_frontend.repository_arn]}]})
  policy_name     = "nl-query-tool-dev-push-to-ecr"
}
module "policy_attachment" {
  source     = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/iam/policy_attachment?ref=ff52f65e7bf0feda762574ae1609231a25c852f3"
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
  role_name  = module.role.role_name
}
module "policy_attachment_1" {
  source     = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/iam/policy_attachment?ref=ff52f65e7bf0feda762574ae1609231a25c852f3"
  policy_arn = module.policy.policy_arn
  role_name  = module.role.role_name
}
module "policy_attachment_2" {
  source     = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/iam/policy_attachment?ref=ff52f65e7bf0feda762574ae1609231a25c852f3"
  policy_arn = module.policy_1.policy_arn
  role_name  = module.role_1.role_name
}
module "policy_attachment_3" {
  source     = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/iam/policy_attachment?ref=ff52f65e7bf0feda762574ae1609231a25c852f3"
  policy_arn = module.policy_2.policy_arn
  role_name  = module.role_1.role_name
}
module "policy_attachment_4" {
  source     = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/iam/policy_attachment?ref=ff52f65e7bf0feda762574ae1609231a25c852f3"
  policy_arn = module.policy_3.policy_arn
  role_name  = module.role_1.role_name
}
module "policy_attachment_6" {
  source     = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/iam/policy_attachment?ref=ff52f65e7bf0feda762574ae1609231a25c852f3"
  policy_arn = module.policy_5.policy_arn
  role_name  = module.role_3.role_name
}
module "rds_sg" {
  source = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/ec2/security_group?ref=7786a6b944ff99c1f5e848f327f6512416c710aa"
  ingress_rules = [{
    from_port       = 5432
    protocol        = "tcp"
    security_groups = [module.backend_sg.security_group_id]
    to_port         = 5432
  }]
  name   = "nl-query-tool-dev-rds"
  vpc_id = module.vpc.id
}
module "role" {
  source             = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/iam/role?ref=254a248d30bd941f8c2f2b1c54707f50a9a67493"
  assume_role_policy = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"ecs-tasks.amazonaws.com\"},\"Action\":\"sts:AssumeRole\"}]}"
  name               = "nl-query-tool-dev-ecs-exec-role"
}
module "role_1" {
  source             = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/iam/role?ref=254a248d30bd941f8c2f2b1c54707f50a9a67493"
  assume_role_policy = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"ecs-tasks.amazonaws.com\"},\"Action\":\"sts:AssumeRole\"}]}"
  name               = "nl-query-tool-dev-ecs-task-role"
  tags = {
    role = "task"
  }
}
module "role_3" {
  source             = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/iam/role?ref=254a248d30bd941f8c2f2b1c54707f50a9a67493"
  assume_role_policy = jsonencode({"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Federated":"arn:aws:iam::941377155192:oidc-provider/token.actions.githubusercontent.com"},"Action":"sts:AssumeRoleWithWebIdentity","Condition":{"StringLike":{"token.actions.githubusercontent.com:sub":"repo:skarthik1015/NLviz:*"}}}]})
  name               = "nl-query-tool-dev-github-deploy-role"
}
module "s3_schemas" {
  source = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/s3/bucket?ref=9451c7cffba19f0d891dd19212e0e5c54b4d6ab0"
  bucket = "nl-query-tool-dev-schemas"
}
module "s3_uploads" {
  source = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/s3/bucket?ref=9451c7cffba19f0d891dd19212e0e5c54b4d6ab0"
  bucket = "nl-query-tool-dev-uploads"
}
module "secret_1" {
  source = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/secretsmanager/secret?ref=9451c7cffba19f0d891dd19212e0e5c54b4d6ab0"
  name   = "nl-query-tool/rds/credentials"
}
module "secret_anthropic" {
  source = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/secretsmanager/secret?ref=9451c7cffba19f0d891dd19212e0e5c54b4d6ab0"
  name   = "nl-query-tool/anthropic/api-key"
}
module "secret_openai" {
  source = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/secretsmanager/secret?ref=9451c7cffba19f0d891dd19212e0e5c54b4d6ab0"
  name   = "nl-query-tool/openai/api-key"
}
module "service" {
  source             = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/ecs/service?ref=7786a6b944ff99c1f5e848f327f6512416c710aa"
  assign_public_ip   = false
  cluster_id         = module.cluster.id
  container_name     = "backend"
  container_port     = 8000
  desired_count      = 1
  launch_type        = "FARGATE"
  name               = "nl-query-tool-dev-backend"
  security_group_ids = [module.backend_sg.security_group_id]
  subnet_ids         = module.vpc.private_subnet_ids
  target_group_arn   = module.target_group.arn
  task_definition    = module.task_definition.arn
}
module "service_1" {
  source             = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/ecs/service?ref=7786a6b944ff99c1f5e848f327f6512416c710aa"
  assign_public_ip   = false
  cluster_id         = module.cluster.id
  container_name     = "frontend"
  container_port     = 3000
  desired_count      = 1
  launch_type        = "FARGATE"
  name               = "nl-query-tool-dev-frontend"
  security_group_ids = [module.frontend_sg.security_group_id]
  subnet_ids         = module.vpc.private_subnet_ids
  target_group_arn   = module.target_group_1.arn
  task_definition    = module.task_definition_1.arn
}
module "subnet_group_1" {
  source      = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/rds/subnet_group?ref=9451c7cffba19f0d891dd19212e0e5c54b4d6ab0"
  name        = "nl-query-tool-dev"
  description = "nl-query-tool-dev RDS subnet group"
  subnet_ids  = module.vpc.private_subnet_ids
}
module "target_group" {
  source = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/alb/target_group?ref=7786a6b944ff99c1f5e848f327f6512416c710aa"
  name   = "nl-query-tool-dev-backend"
  port   = 8000
  vpc_id = module.vpc.id
}
module "target_group_1" {
  source = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/alb/target_group?ref=7786a6b944ff99c1f5e848f327f6512416c710aa"
  name   = "nl-query-tool-dev-frontend"
  port   = 3000
  vpc_id = module.vpc.id
}
module "task_definition" {
  source = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/ecs/task_definition?ref=7786a6b944ff99c1f5e848f327f6512416c710aa"
  container_definitions = jsonencode([{
    name      = "backend"
    image     = "${module.ecr_backend.repository_url}:latest"
    essential = true
    portMappings = [{ containerPort = 8000, protocol = "tcp" }]
    environment = [
      { name = "ENVIRONMENT",         value = "production" },
      { name = "SECRET_BACKEND",      value = "aws_secrets_manager" },
      { name = "SECRET_PREFIX",       value = "nl-query-tool" },
      { name = "AWS_REGION",          value = "us-east-1" },
      { name = "UPLOAD_BUCKET",       value = module.s3_uploads.id },
      { name = "SCHEMA_BUCKET",       value = module.s3_schemas.id },
      { name = "API_PREFIX",          value = "/api" },
      { name = "RATE_LIMIT_RPM",      value = "60" },
      { name = "AUTO_MIGRATE",        value = "true" },
      { name = "LLM_PROVIDER",        value = "openai" },
      { name = "LLM_MODEL",           value = "gpt-4.1-mini" },
      { name = "DEV_USER_ID",         value = "dev" },
      { name = "CORS_ALLOW_ORIGINS",  value = "http://${module.load_balancer.dns_name}" },
    ]
    secrets = [
      { name = "OPENAI_API_KEY",   valueFrom = module.secret_openai.arn },
      { name = "SECRET_STORE_KEY", valueFrom = module.secret_1.arn },
    ]
    healthCheck = {
      command     = ["CMD-SHELL", "curl -f http://localhost:8000/healthz || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 15
    }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = module.log_backend.log_group_name
        "awslogs-region"        = "us-east-1"
        "awslogs-stream-prefix" = "ecs"
      }
    }
  }])
  cpu                = 512
  execution_role_arn = module.role.role_arn
  family             = "nl-query-tool-dev-backend"
  memory             = 1024
  task_role_arn      = module.role_1.role_arn
}
module "task_definition_1" {
  source                = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/ecs/task_definition?ref=7786a6b944ff99c1f5e848f327f6512416c710aa"
  container_definitions = jsonencode([{ name = "frontend", image = module.ecr_frontend.repository_url, essential = true, portMappings = [{ containerPort = 3000, protocol = "tcp" }], logConfiguration = { logDriver = "awslogs", options = { "awslogs-group" = module.log_frontend.log_group_name, "awslogs-region" = "us-east-1", "awslogs-stream-prefix" = "ecs" } } }])
  cpu                   = 256
  execution_role_arn    = module.role.role_arn
  family                = "nl-query-tool-dev-frontend"
  memory                = 512
  task_role_arn         = module.role_1.role_arn
}
module "vpc" {
  source = "git::https://github.com/lace-cloud/registry-tf.git//modules/aws/vpc?ref=9451c7cffba19f0d891dd19212e0e5c54b4d6ab0"
  name   = "nl-query-tool-dev"
}
