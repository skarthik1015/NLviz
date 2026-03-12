terraform {
  required_providers {
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }
}

locals {
  tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

resource "random_password" "db" {
  length           = 32
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "aws_db_subnet_group" "main" {
  name        = "${var.project}-${var.environment}-db-subnet-group"
  subnet_ids  = var.private_subnet_ids
  description = "Subnet group for ${var.project} RDS instance"
  tags        = merge(local.tags, { Name = "${var.project}-${var.environment}-db-subnet-group" })
}

resource "aws_db_instance" "main" {
  identifier     = "${var.project}-${var.environment}-postgres"
  engine         = "postgres"
  engine_version = "15.4"
  instance_class = var.instance_class

  allocated_storage     = var.allocated_storage_gb
  max_allocated_storage = var.allocated_storage_gb * 5  # auto-scaling up to 5x initial
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = var.db_name
  username = var.db_username
  password = random_password.db.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [var.rds_sg_id]

  backup_retention_period      = 7
  backup_window                = "03:00-04:00"
  maintenance_window           = "Mon:04:00-Mon:05:00"
  deletion_protection          = true
  skip_final_snapshot          = false
  final_snapshot_identifier    = "${var.project}-${var.environment}-final-snapshot"
  performance_insights_enabled = true
  multi_az                     = false  # single-AZ for dev; enable for production

  tags = merge(local.tags, { Name = "${var.project}-${var.environment}-postgres" })

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [password]  # managed by random_password; don't recreate on drift
  }
}

# Store all credentials in Secrets Manager so ECS can inject DATABASE_URL at startup

resource "aws_secretsmanager_secret" "rds_credentials" {
  name        = "${var.project}/rds/credentials"
  description = "RDS PostgreSQL credentials for ${var.project}"
  tags        = merge(local.tags, { Purpose = "rds-credentials" })

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_secretsmanager_secret_version" "rds_credentials" {
  secret_id = aws_secretsmanager_secret.rds_credentials.id
  secret_string = jsonencode({
    host              = aws_db_instance.main.address
    port              = tostring(aws_db_instance.main.port)
    dbname            = var.db_name
    username          = var.db_username
    password          = random_password.db.result
    connection_string = "postgresql://${var.db_username}:${random_password.db.result}@${aws_db_instance.main.address}:${aws_db_instance.main.port}/${var.db_name}"
  })

  lifecycle {
    ignore_changes = [secret_string]  # prevent Terraform from overwriting if rotated
  }
}
