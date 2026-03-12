locals {
  tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

# Anthropic API key secret — value must be set manually after apply:
#   aws secretsmanager put-secret-value \
#     --secret-id <arn> \
#     --secret-string '{"api_key":"sk-ant-..."}'

resource "aws_secretsmanager_secret" "anthropic_api_key" {
  name        = "${var.project}/anthropic/api-key"
  description = "Anthropic API key for LLM intent mapping"

  tags = merge(local.tags, { Purpose = "llm-api-key" })

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_secretsmanager_secret" "openai_api_key" {
  name        = "${var.project}/openai/api-key"
  description = "OpenAI API key for LLM intent mapping"

  tags = merge(local.tags, { Purpose = "llm-api-key" })

  lifecycle {
    prevent_destroy = true
  }
}
