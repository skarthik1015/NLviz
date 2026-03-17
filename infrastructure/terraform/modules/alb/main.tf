locals {
  tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
  }
  auth_enabled = (
    var.certificate_arn != null &&
    var.cognito_user_pool_arn != null &&
    var.cognito_user_pool_client_id != null &&
    var.cognito_user_pool_domain != null
  )
}

resource "aws_lb" "main" {
  name               = "${var.project}-${var.environment}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [var.alb_sg_id]
  subnets            = var.public_subnet_ids

  enable_deletion_protection = false

  tags = merge(local.tags, { Name = "${var.project}-${var.environment}-alb" })
}

resource "aws_lb_target_group" "backend" {
  name        = "${var.project}-${var.environment}-backend-tg"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

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

  deregistration_delay = 30

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

resource "aws_lb_listener" "https" {
  count             = local.auth_enabled ? 1 : 0
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type  = "authenticate-cognito"
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

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  dynamic "default_action" {
    for_each = local.auth_enabled ? [1] : []

    content {
      type = "redirect"

      redirect {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }

  dynamic "default_action" {
    for_each = local.auth_enabled ? [] : [1]

    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.frontend.arn
    }
  }

  tags = merge(local.tags, { Name = "${var.project}-${var.environment}-http-listener" })
}

resource "aws_lb_listener_rule" "healthz" {
  listener_arn = local.auth_enabled ? aws_lb_listener.https[0].arn : aws_lb_listener.http.arn
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

resource "aws_lb_listener_rule" "api" {
  listener_arn = local.auth_enabled ? aws_lb_listener.https[0].arn : aws_lb_listener.http.arn
  priority     = 10

  condition {
    path_pattern {
      values = ["/api/*"]
    }
  }

  dynamic "action" {
    for_each = local.auth_enabled ? [1] : []

    content {
      type  = "authenticate-cognito"
      order = 1

      authenticate_cognito {
        user_pool_arn       = var.cognito_user_pool_arn
        user_pool_client_id = var.cognito_user_pool_client_id
        user_pool_domain    = var.cognito_user_pool_domain
      }
    }
  }

  action {
    type             = "forward"
    order            = local.auth_enabled ? 2 : 1
    target_group_arn = aws_lb_target_group.backend.arn
  }

  tags = merge(local.tags, { Name = "${var.project}-${var.environment}-api-rule" })
}
