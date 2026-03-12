terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }

  # Replace bucket/dynamodb_table/region with values output by bootstrap/main.tf
  backend "s3" {
    bucket         = "nl-query-tool-terraform-state-941377155192"
    key            = "environments/dev/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "nl-query-tool-terraform-locks"
    encrypt        = true
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

# ── Modules ───────────────────────────────────────────────────────────

module "networking" {
  source      = "../../modules/networking"
  project     = var.project
  environment = var.environment
}

module "ecr" {
  source      = "../../modules/ecr"
  project     = var.project
  environment = var.environment
}

module "monitoring" {
  source      = "../../modules/monitoring"
  project     = var.project
  environment = var.environment
}

module "secrets" {
  source      = "../../modules/secrets"
  project     = var.project
  environment = var.environment
}

module "rds" {
  source             = "../../modules/rds"
  project            = var.project
  environment        = var.environment
  private_subnet_ids = module.networking.private_subnet_ids
  rds_sg_id          = module.networking.rds_sg_id
}

# ALB is created before S3 (S3 CORS needs ALB DNS) and before ECS (ECS needs TG ARNs)
module "alb" {
  source            = "../../modules/alb"
  project           = var.project
  environment       = var.environment
  vpc_id            = module.networking.vpc_id
  public_subnet_ids = module.networking.public_subnet_ids
  alb_sg_id         = module.networking.alb_sg_id
}

module "s3" {
  source       = "../../modules/s3"
  project      = var.project
  environment  = var.environment
  alb_dns_name = module.alb.alb_dns_name
}

module "iam" {
  source               = "../../modules/iam"
  project              = var.project
  environment          = var.environment
  aws_region           = var.aws_region
  rds_secret_arn       = module.rds.db_secret_arn
  anthropic_secret_arn = module.secrets.anthropic_secret_arn
  openai_secret_arn    = module.secrets.openai_secret_arn
  uploads_bucket_arn   = module.s3.uploads_bucket_arn
  schemas_bucket_arn   = module.s3.schemas_bucket_arn
  github_org           = var.github_org
  github_repo          = var.github_repo
  ecr_backend_arn      = module.ecr.backend_repository_arn
  ecr_frontend_arn     = module.ecr.frontend_repository_arn
}

module "ecs" {
  source      = "../../modules/ecs"
  project     = var.project
  environment = var.environment
  aws_region  = var.aws_region

  private_subnet_ids = module.networking.private_subnet_ids
  backend_sg_id      = module.networking.backend_sg_id
  frontend_sg_id     = module.networking.frontend_sg_id

  backend_target_group_arn  = module.alb.backend_target_group_arn
  frontend_target_group_arn = module.alb.frontend_target_group_arn

  task_execution_role_arn = module.iam.task_execution_role_arn
  task_role_arn           = module.iam.task_role_arn

  backend_image  = "${module.ecr.backend_repository_url}:${var.backend_image_tag}"
  frontend_image = "${module.ecr.frontend_repository_url}:${var.frontend_image_tag}"

  backend_log_group  = module.monitoring.backend_log_group_name
  frontend_log_group = module.monitoring.frontend_log_group_name

  rds_secret_arn    = module.rds.db_secret_arn
  openai_secret_arn = module.secrets.openai_secret_arn

  uploads_bucket_name = module.s3.uploads_bucket_name
  schemas_bucket_name = module.s3.schemas_bucket_name
  alb_dns_name        = module.alb.alb_dns_name
}
