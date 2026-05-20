terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Matches the personal convention: tf-state bucket, key per project.
  # Credentials come from the standard AWS provider chain (env vars first,
  # then ~/.aws/credentials). Locally: set $env:AWS_PROFILE = "k8s-lab".
  # In GitHub Actions: OIDC-assumed creds are injected as env vars.
  backend "s3" {
    bucket  = "tf-state-yury"
    key     = "gmail-job-triage-agent/terraform.tfstate"
    region  = "us-east-1"
    encrypt = true
  }
}

provider "aws" {
  region = var.aws_region

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

# ---- SSM Parameter Store (SecureString) -----------------------------------
# One parameter per credential, grouped under /${project_name}/.
# Standard SecureString tier is free (<=4 KB, <=10k params/account) and uses
# the AWS-managed `alias/aws/ssm` KMS key at no extra cost.

locals {
  secret_keys = [
    "anthropic_api_key",
    "gmail_client_id",
    "gmail_client_secret",
    "gmail_refresh_token",
    "telegram_bot_token",
    "telegram_chat_id",
  ]
  ssm_path = "/${var.project_name}"

  # Must match the backend "s3" key above. Surfaced as a local so the
  # github_actions.tf IAM policy can grant access to exactly this prefix.
  tfstate_bucket = "tf-state-yury"
  tfstate_prefix = "gmail-job-triage-agent"
}

resource "aws_ssm_parameter" "secret" {
  for_each = toset(local.secret_keys)

  name        = "${local.ssm_path}/${each.key}"
  description = "Credential for gmail-job-triage-agent — populate via aws ssm put-parameter."
  type        = "SecureString"
  value       = "REPLACE_ME"

  lifecycle {
    # Real values are populated out-of-band (see README §6) — never managed by TF.
    ignore_changes = [value]
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
  # AWS Lambda max. A 250-email backfill with rate-limit-respecting batches
  # takes roughly 7-8 minutes; this gives margin for slow Gmail pagination.
  timeout = 900

  environment {
    variables = {
      SSM_PARAM_PATH   = local.ssm_path
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
