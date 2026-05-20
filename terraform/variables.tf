variable "project_name" {
  description = "Prefix used for all resource names."
  type        = string
  default     = "email-agent"
}

variable "aws_region" {
  description = "Region for all resources."
  type        = string
  default     = "us-east-1"
}

variable "image_uri" {
  description = "Full ECR image URI for the Lambda container (e.g. <acct>.dkr.ecr.<region>.amazonaws.com/email-agent:<tag>). Empty during the local bootstrap apply; GitHub Actions sets it on every push."
  type        = string
  default     = ""
}

variable "schedule_expression" {
  description = "EventBridge cron expression in UTC. Default = 12:00 UTC (08:00 Miami EDT)."
  type        = string
  default     = "cron(0 12 * * ? *)"
}

variable "log_retention_days" {
  description = "CloudWatch log retention."
  type        = number
  default     = 14
}

variable "log_level" {
  description = "Python log level inside the Lambda."
  type        = string
  default     = "INFO"
}

variable "tags" {
  description = "Tags applied to all resources."
  type        = map(string)
  default = {
    project = "gmail-job-triage-agent"
    owner   = "yurykuvaev"
  }
}
