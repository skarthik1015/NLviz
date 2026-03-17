locals {
  tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
  }

  callback_urls = [
    "https://${var.domain_name}/oauth2/idpresponse",
  ]
  logout_urls = [
    "https://${var.domain_name}",
  ]
}

# ── Cognito User Pool ────────────────────────────────────────────────

resource "aws_cognito_user_pool" "main" {
  name = "${var.project}-${var.environment}-users"

  # Sign-in: email only
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  password_policy {
    minimum_length                   = 8
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 7
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  schema {
    name                = "email"
    attribute_data_type = "String"
    required            = true
    mutable             = true

    string_attribute_constraints {
      min_length = 1
      max_length = 256
    }
  }

  tags = merge(local.tags, { Name = "${var.project}-${var.environment}-user-pool" })
}

# ── Cognito Hosted UI Domain ─────────────────────────────────────────

resource "aws_cognito_user_pool_domain" "main" {
  domain       = var.cognito_domain_prefix
  user_pool_id = aws_cognito_user_pool.main.id
}

# ── App Client (for ALB OIDC integration) ────────────────────────────

resource "aws_cognito_user_pool_client" "alb" {
  name         = "${var.project}-${var.environment}-alb-client"
  user_pool_id = aws_cognito_user_pool.main.id

  generate_secret = true # required for ALB authenticate-cognito action

  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  allowed_oauth_flows_user_pool_client = true
  supported_identity_providers         = ["COGNITO"]

  callback_urls = local.callback_urls
  logout_urls   = local.logout_urls

  # Token validity
  access_token_validity  = 1  # hours
  id_token_validity      = 1  # hours
  refresh_token_validity = 30 # days

  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }
}
