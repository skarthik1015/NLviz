locals {
  tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
  }
  cluster_name = "${var.project}-${var.environment}"
}

# ── ECS Cluster ───────────────────────────────────────────────────────

resource "aws_ecs_cluster" "main" {
  name = local.cluster_name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = merge(local.tags, { Name = local.cluster_name })
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    base              = 1
    weight            = 100
    capacity_provider = "FARGATE"
  }
}

# ── Backend Task Definition ───────────────────────────────────────────

resource "aws_ecs_task_definition" "backend" {
  family                   = "${var.project}-${var.environment}-backend"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.backend_cpu)
  memory                   = tostring(var.backend_memory_mb)
  execution_role_arn       = var.task_execution_role_arn
  task_role_arn            = var.task_role_arn

  container_definitions = jsonencode([{
    name      = "backend"
    image     = var.backend_image
    essential = true

    portMappings = [{
      containerPort = 8000
      protocol      = "tcp"
    }]

    environment = [
      { name = "API_PREFIX",           value = "/api" },
      { name = "ENVIRONMENT",          value = var.environment },
      { name = "LOG_LEVEL",            value = "INFO" },
      { name = "CORS_ALLOW_ORIGINS",   value = "http://${var.alb_dns_name}" },
      { name = "SECRET_BACKEND",       value = "aws_secrets_manager" },
      { name = "SECRET_PREFIX",        value = var.project },
      { name = "UPLOAD_BUCKET",        value = var.uploads_bucket_name },
      { name = "SCHEMA_BUCKET",        value = var.schemas_bucket_name },
      { name = "AWS_REGION",           value = var.aws_region },
      { name = "AUTO_MIGRATE",         value = "true" },
    ]

    # Secrets injected at container startup from Secrets Manager.
    # DATABASE_URL is the full connection_string from the RDS secret JSON.
    secrets = [
      {
        name      = "ANTHROPIC_API_KEY"
        valueFrom = "${var.anthropic_secret_arn}:api_key::"
      },
      {
        name      = "DATABASE_URL"
        valueFrom = "${var.rds_secret_arn}:connection_string::"
      },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = var.backend_log_group
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "ecs"
      }
    }

    healthCheck = {
      command     = ["CMD-SHELL", "curl -f http://localhost:8000/healthz || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 60
    }
  }])

  tags = merge(local.tags, { Name = "${var.project}-${var.environment}-backend-task" })
}

# ── Frontend Task Definition ──────────────────────────────────────────

resource "aws_ecs_task_definition" "frontend" {
  family                   = "${var.project}-${var.environment}-frontend"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.frontend_cpu)
  memory                   = tostring(var.frontend_memory_mb)
  execution_role_arn       = var.task_execution_role_arn
  task_role_arn            = var.task_role_arn

  container_definitions = jsonencode([{
    name      = "frontend"
    image     = var.frontend_image
    essential = true

    portMappings = [{
      containerPort = 3000
      protocol      = "tcp"
    }]

    environment = [
      # NEXT_PUBLIC_API_BASE_URL is baked into the image at build time via Docker build-arg.
      # No runtime env override needed for this value.
      { name = "ENVIRONMENT", value = var.environment },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = var.frontend_log_group
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "ecs"
      }
    }

    healthCheck = {
      command     = ["CMD-SHELL", "wget -qO- http://localhost:3000/ > /dev/null || exit 1"]
      interval    = 30
      timeout     = 10
      retries     = 3
      startPeriod = 90
    }
  }])

  tags = merge(local.tags, { Name = "${var.project}-${var.environment}-frontend-task" })
}

# ── ECS Services ──────────────────────────────────────────────────────

resource "aws_ecs_service" "backend" {
  name            = "${var.project}-${var.environment}-backend"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.backend.arn
  desired_count   = var.backend_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.backend_sg_id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = var.backend_target_group_arn
    container_name   = "backend"
    container_port   = 8000
  }

  deployment_minimum_healthy_percent = 50
  deployment_maximum_percent         = 200

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # Ignore task definition changes — CI/CD updates them directly via aws ecs update-service
  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }

  depends_on = [var.backend_target_group_arn]
  tags       = merge(local.tags, { Name = "${var.project}-${var.environment}-backend-svc" })
}

resource "aws_ecs_service" "frontend" {
  name            = "${var.project}-${var.environment}-frontend"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.frontend.arn
  desired_count   = var.frontend_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.frontend_sg_id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = var.frontend_target_group_arn
    container_name   = "frontend"
    container_port   = 3000
  }

  deployment_minimum_healthy_percent = 50
  deployment_maximum_percent         = 200

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }

  depends_on = [var.frontend_target_group_arn]
  tags       = merge(local.tags, { Name = "${var.project}-${var.environment}-frontend-svc" })
}

# ── Auto-scaling (backend only) ───────────────────────────────────────

resource "aws_appautoscaling_target" "backend" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.backend.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = 1
  max_capacity       = 4
}

resource "aws_appautoscaling_policy" "backend_cpu" {
  name               = "${var.project}-${var.environment}-backend-cpu-scaling"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.backend.service_namespace
  resource_id        = aws_appautoscaling_target.backend.resource_id
  scalable_dimension = aws_appautoscaling_target.backend.scalable_dimension

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = 70.0
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
  }
}
