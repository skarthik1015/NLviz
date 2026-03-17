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

# ── HTTPS Listener + Cognito Auth ────────────────────────────────────
#
# Traffic flow (authenticated):
#   GET /healthz → backend (no auth — ALB health check)
#   GET /api/*   → Cognito auth → backend (FastAPI, port 8000)
#   GET /*       → Cognito auth → frontend (Next.js, port 3000)
#
# The ALB handles the full OAuth2 code grant flow with Cognito Hosted UI.
# After successful login, ALB sets AWSELBAuthSessionCookie-* and injects
# x-amzn-oidc-identity (user sub) and x-amzn-oidc-data (signed JWT) headers.

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  # Default: authenticate via Cognito, then forward to frontend
  default_action {
    type = "authenticate-cognito"
    order = 1

    authenticate_cognito {
      user_pool_arn       = var.cognito_user_pool_arn
      user_pool_client_id = var.cognito_user_pool_client_id
      user_pool_domain    = var.cognito_user_pool_domain
    }
  }

  default_action {
    type             = "forward"
    order            = 2
    target_group_arn = aws_lb_target_group.frontend.arn
  }

  tags = merge(local.tags, { Name = "${var.project}-${var.environment}-https-listener" })
}

# Health check — no auth (priority 5, evaluated first)
resource "aws_lb_listener_rule" "healthz" {
  listener_arn = aws_lb_listener.https.arn
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

# API routes — authenticate then forward to backend
resource "aws_lb_listener_rule" "api" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 10

  condition {
    path_pattern {
      values = ["/api/*"]
    }
  }

  action {
    type  = "authenticate-cognito"
    order = 1

    authenticate_cognito {
      user_pool_arn       = var.cognito_user_pool_arn
      user_pool_client_id = var.cognito_user_pool_client_id
      user_pool_domain    = var.cognito_user_pool_domain
    }
  }

  action {
    type             = "forward"
    order            = 2
    target_group_arn = aws_lb_target_group.backend.arn
  }

  tags = merge(local.tags, { Name = "${var.project}-${var.environment}-api-rule" })
}

# ── HTTP → HTTPS Redirect ────────────────────────────────────────────

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"

    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }

  tags = merge(local.tags, { Name = "${var.project}-${var.environment}-http-redirect" })
}
