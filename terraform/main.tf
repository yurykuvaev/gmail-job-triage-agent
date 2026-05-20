terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Matches the personal convention: tf-state bucket, key per project.
  backend "s3" {
    bucket  = "tf-state-yury"
    key     = "gmail-job-triage-agent/terraform.tfstate"
    region  = "us-east-1"
    profile = "k8s-lab"
    encrypt = true
  }
}

provider "aws" {
  region  = var.aws_region
  profile = "k8s-lab"

  default_tags {
    tags = var.tags
  }
}

# ---- ECR ------------------------------------------------------------------

resource "aws_ecr_repository" "lambda" {
  name                 = var.project_name
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "lambda" {
  repository = aws_ecr_repository.lambda.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 5 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 5
      }
      action = { type = "expire" }
    }]
  })
}

# ---- Secrets Manager ------------------------------------------------------

resource "aws_secretsmanager_secret" "app" {
  name                    = "${var.project_name}/credentials"
  description             = "Anthropic + Gmail OAuth + Telegram bot credentials."
  recovery_window_in_days = 0
}

# Placeholder so terraform apply succeeds on a fresh account. Populate via:
#   aws secretsmanager put-secret-value --secret-id <arn> --secret-string file://secret.json
resource "aws_secretsmanager_secret_version" "placeholder" {
  secret_id = aws_secretsmanager_secret.app.id
  secret_string = jsonencode({
    anthropic_api_key   = "REPLACE_ME"
    gmail_client_id     = "REPLACE_ME"
    gmail_client_secret = "REPLACE_ME"
    gmail_refresh_token = "REPLACE_ME"
    telegram_bot_token  = "REPLACE_ME"
    telegram_chat_id    = "REPLACE_ME"
  })

  lifecycle {
    # Avoid clobbering real values on subsequent applies.
    ignore_changes = [secret_string]
  }
}

# ---- DynamoDB -------------------------------------------------------------

resource "aws_dynamodb_table" "state" {
  name         = "${var.project_name}-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "message_id"

  attribute {
    name = "message_id"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = false
  }
}

# ---- CloudWatch log group -------------------------------------------------

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.project_name}"
  retention_in_days = var.log_retention_days
}

# ---- Lambda ---------------------------------------------------------------

resource "aws_lambda_function" "agent" {
  count = var.image_uri == "" ? 0 : 1

  function_name = var.project_name
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = var.image_uri
  architectures = ["arm64"]
  memory_size   = 512
  timeout       = 300

  environment {
    variables = {
      SECRETS_ARN      = aws_secretsmanager_secret.app.arn
      STATE_TABLE_NAME = aws_dynamodb_table.state.name
      LOG_LEVEL        = var.log_level
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda,
    aws_iam_role_policy.lambda,
  ]
}

# ---- EventBridge schedule -------------------------------------------------

resource "aws_cloudwatch_event_rule" "daily" {
  name                = "${var.project_name}-daily"
  description         = "Daily job-search email digest trigger."
  schedule_expression = var.schedule_expression
}

resource "aws_cloudwatch_event_target" "daily" {
  count     = var.image_uri == "" ? 0 : 1
  rule      = aws_cloudwatch_event_rule.daily.name
  target_id = "lambda"
  arn       = aws_lambda_function.agent[0].arn
}

resource "aws_lambda_permission" "events" {
  count         = var.image_uri == "" ? 0 : 1
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.agent[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.daily.arn
}

# ---- Errors alarm (SNS wiring intentionally commented out) ----------------

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  count               = var.image_uri == "" ? 0 : 1
  alarm_name          = "${var.project_name}-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.agent[0].function_name
  }

  # alarm_actions = [aws_sns_topic.alerts.arn]  # wire SNS yourself if desired
}
