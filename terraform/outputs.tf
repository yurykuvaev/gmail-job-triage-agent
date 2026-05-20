output "ecr_repository_url" {
  description = "Push the Lambda image here."
  value       = aws_ecr_repository.lambda.repository_url
}

output "ssm_param_path" {
  description = "SSM Parameter Store prefix holding the six credentials (populate via aws ssm put-parameter; see README §6)."
  value       = local.ssm_path
}

output "ssm_param_names" {
  description = "Full SSM parameter names that must be populated before the Lambda will run."
  value       = sort([for p in aws_ssm_parameter.secret : p.name])
}

output "state_table_name" {
  value = aws_dynamodb_table.state.name
}

output "lambda_function_name" {
  description = "Empty until the first image is pushed and image_uri is set."
  value       = try(aws_lambda_function.agent[0].function_name, null)
}

output "log_group_name" {
  value = aws_cloudwatch_log_group.lambda.name
}
