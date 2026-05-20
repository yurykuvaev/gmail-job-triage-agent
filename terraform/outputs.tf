output "ecr_repository_url" {
  description = "Push the Lambda image here."
  value       = aws_ecr_repository.lambda.repository_url
}

output "secret_arn" {
  description = "Populate this secret with real credentials (see README)."
  value       = aws_secretsmanager_secret.app.arn
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
