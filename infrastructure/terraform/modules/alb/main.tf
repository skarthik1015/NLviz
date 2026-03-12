locals {
  tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

# ── Application Load Balancer ─────────────────────────────────────────

resource "aws_lb" "main" {
  name               = "${var.project}-${var.environment}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [var.alb_sg_id]
  subnets            = var.public_subnet_ids

  enable_deletion_protection = false  # set true for production

  tags = merge(local.tags, { Name = "${var.project}-${var.environment}-alb" })
}

# ── Target Groups ─────────────────────────────────────────────────────

resource "aws_lb_target_group" "backend" {
  name        = "${var.project}-${var.environment}-backend-tg"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"  # required for ECS Fargate (awsvpc mode)

  health_check {
    enabled             = true
    path                = "/healthz"
    port                = "traffic-port"
    protocol            = "HTTP"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 30
    matcher             = "200"
  }

  deregistration_delay = 30  # faster rolling deploys

  tags = merge(local.tags, { Name = "${var.project}-${var.environment}-backend-tg" })
}

resource "aws_lb_target_group" "frontend" {
  name        = "${var.project}-${var.environment}-frontend-tg"
  port        = 3000
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    enabled             = true
    path                = "/"
    port                = "traffic-port"
    protocol            = "HTTP"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 30
    matcher             = "200"
  }

  deregistration_delay = 30

  tags = merge(local.tags, { Name = "${var.project}-${var.environment}-frontend-tg" })
}

# ── HTTP Listener + Routing Rules ────────────────────────────────────
#
# Traffic flow:
#   GET /api/*  → backend (FastAPI, port 8000) — ALB forwards full path including /api
#   GET /*      → frontend (Next.js, port 3000)
#
# FastAPI must be configured with API_PREFIX=/api so it registers routes
# under /api/chat, /api/schema, etc. /healthz remains at root for ALB health checks.

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  # Default: all unmatched traffic goes to the frontend
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.frontend.arn
  }

  tags = merge(local.tags, { Name = "${var.project}-${var.environment}-http-listener" })
}

resource "aws_lb_listener_rule" "api" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 10

  condition {
    path_pattern {
      values = ["/api/*"]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.backend.arn
  }

  tags = merge(local.tags, { Name = "${var.project}-${var.environment}-api-rule" })
}

# Health check endpoint is at /healthz (no prefix), also routed to backend
resource "aws_lb_listener_rule" "healthz" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 5

  condition {
    path_pattern {
      values = ["/healthz"]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.backend.arn
  }

  tags = merge(local.tags, { Name = "${var.project}-${var.environment}-healthz-rule" })
}
