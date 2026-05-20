# GitHub Actions OIDC deploy role.
#
# Security model: GHA workers from this specific repo+branch get a short-lived
# STS session via OIDC — no long-lived AWS keys ever stored as repo secrets.
# Trust is pinned by `sub` claim, so even a different fork or branch in the
# same account can't assume this role.

variable "github_repo" {
  description = "GitHub owner/repo allowed to assume the CI role."
  type        = string
  default     = "yurykuvaev/gmail-job-triage-agent"
}

variable "github_branch" {
  description = "Branch allowed to assume the role. Use '*' to allow any branch."
  type        = string
  default     = "main"
}

# One OIDC provider per AWS account, keyed by issuer URL. If you already have
# this from another project, terraform apply will error — import it instead:
#   terraform import aws_iam_openid_connect_provider.github \
#     arn:aws:iam::<account>:oidc-provider/token.actions.githubusercontent.com
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]

  # AWS no longer validates this since 2023 (cert chain is checked instead) but
  # the field is still required. GitHub's well-known thumbprint:
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

resource "aws_iam_role" "github_actions_deploy" {
  name        = "${var.project_name}-gha-deploy"
  description = "Assumed by GitHub Actions in ${var.github_repo} (branch ${var.github_branch}) to build the image and run terraform apply."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = aws_iam_openid_connect_provider.github.arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          "token.actions.githubusercontent.com:sub" = var.github_branch == "*" ? "repo:${var.github_repo}:*" : "repo:${var.github_repo}:ref:refs/heads/${var.github_branch}"
        }
      }
    }]
  })
}

# Permissions: scoped to project-owned resources where possible. Not minimal-
# possible because `terraform apply` needs to read every resource type it
# manages to compute a plan; tightening this further would require switching
# to a separate plan/apply role split, which is overkill for one project.
resource "aws_iam_role_policy" "github_actions_deploy" {
  name = "${var.project_name}-gha-deploy"
  role = aws_iam_role.github_actions_deploy.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "TerraformStateRW"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject"
        ]
        Resource = ["arn:aws:s3:::tf-state-yury/${var.project_name}/*"]
      },
      {
        Sid      = "TerraformStateListBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = ["arn:aws:s3:::tf-state-yury"]
      },
      {
        Sid      = "EcrAuth"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid      = "EcrRepoFull"
        Effect   = "Allow"
        Action   = ["ecr:*"]
        Resource = [aws_ecr_repository.lambda.arn]
      },
      {
        Sid    = "LambdaFunction"
        Effect = "Allow"
        Action = ["lambda:*"]
        Resource = [
          "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.project_name}"
        ]
      },
      {
        # PassRole is required when (re)creating the Lambda function with the
        # execution role attached. Scoped to the one role we manage.
        Sid    = "IamLambdaExecRole"
        Effect = "Allow"
        Action = [
          "iam:GetRole",
          "iam:GetRolePolicy",
          "iam:ListRolePolicies",
          "iam:ListAttachedRolePolicies",
          "iam:PutRolePolicy",
          "iam:DeleteRolePolicy",
          "iam:TagRole",
          "iam:UntagRole",
          "iam:PassRole",
        ]
        Resource = [aws_iam_role.lambda.arn]
      },
      {
        # Read-only on the GHA role itself + the OIDC provider so terraform's
        # state refresh succeeds. No mutations allowed — those resources are
        # bootstrapped from a local apply, never from CI (prevents priv-esc).
        Sid    = "IamReadSelfBootstrap"
        Effect = "Allow"
        Action = [
          "iam:GetRole",
          "iam:GetRolePolicy",
          "iam:ListRolePolicies",
          "iam:ListAttachedRolePolicies",
          "iam:GetOpenIDConnectProvider",
        ]
        Resource = [
          aws_iam_role.github_actions_deploy.arn,
          aws_iam_openid_connect_provider.github.arn,
        ]
      },
      {
        Sid      = "DynamoTable"
        Effect   = "Allow"
        Action   = ["dynamodb:*"]
        Resource = [aws_dynamodb_table.state.arn]
      },
      {
        Sid    = "LogGroup"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:DeleteLogGroup",
          "logs:DescribeLogGroups",
          "logs:PutRetentionPolicy",
          "logs:TagResource",
          "logs:UntagResource",
          "logs:ListTagsForResource",
        ]
        Resource = [
          aws_cloudwatch_log_group.lambda.arn,
          "${aws_cloudwatch_log_group.lambda.arn}:*",
        ]
      },
      {
        Sid    = "EventBridgeRule"
        Effect = "Allow"
        Action = [
          "events:PutRule",
          "events:DeleteRule",
          "events:DescribeRule",
          "events:PutTargets",
          "events:RemoveTargets",
          "events:ListTargetsByRule",
          "events:TagResource",
          "events:UntagResource",
          "events:ListTagsForResource",
        ]
        Resource = [aws_cloudwatch_event_rule.daily.arn]
      },
      {
        Sid    = "CloudWatchAlarm"
        Effect = "Allow"
        Action = [
          "cloudwatch:DescribeAlarms",
          "cloudwatch:PutMetricAlarm",
          "cloudwatch:DeleteAlarms",
          "cloudwatch:TagResource",
          "cloudwatch:UntagResource",
          "cloudwatch:ListTagsForResource",
        ]
        Resource = [
          "arn:aws:cloudwatch:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:alarm:${var.project_name}-*"
        ]
      },
      {
        Sid    = "SsmParamMetadata"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:GetParameters",
          "ssm:GetParametersByPath",
          "ssm:DescribeParameters",
          "ssm:PutParameter",
          "ssm:DeleteParameter",
          "ssm:ListTagsForResource",
          "ssm:AddTagsToResource",
          "ssm:RemoveTagsFromResource",
        ]
        Resource = [
          "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter${local.ssm_path}/*"
        ]
      },
      {
        Sid      = "KmsViaSsm"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:Encrypt", "kms:DescribeKey"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:ViaService" = "ssm.${data.aws_region.current.name}.amazonaws.com"
          }
        }
      },
      {
        Sid      = "StsCallerIdentity"
        Effect   = "Allow"
        Action   = ["sts:GetCallerIdentity"]
        Resource = "*"
      },
    ]
  })
}

output "gha_deploy_role_arn" {
  description = "Paste this ARN into .github/workflows/deploy.yml as role-to-assume."
  value       = aws_iam_role.github_actions_deploy.arn
}
