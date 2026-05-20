#!/usr/bin/env bash
# Build, push, and apply.
#
# Requirements:
#   - terraform >= 1.5, awscli, docker buildx (for arm64 from x86 host)
#   - AWS profile k8s-lab configured
#   - terraform.tfvars present in terraform/
#
# Usage: scripts/deploy.sh [image_tag]

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TF_DIR="$ROOT_DIR/terraform"
PROFILE="${AWS_PROFILE:-k8s-lab}"
REGION="${AWS_REGION:-us-east-1}"
TAG="${1:-$(date -u +%Y%m%d-%H%M%S)}"

echo "==> terraform init"
terraform -chdir="$TF_DIR" init -upgrade

echo "==> terraform apply (infra only, lambda count=0 until image is pushed)"
terraform -chdir="$TF_DIR" apply -auto-approve -var "image_uri="

ECR_URL=$(terraform -chdir="$TF_DIR" output -raw ecr_repository_url)
IMAGE_URI="${ECR_URL}:${TAG}"

echo "==> docker login -> $ECR_URL"
aws ecr get-login-password --region "$REGION" --profile "$PROFILE" \
  | docker login --username AWS --password-stdin "${ECR_URL%/*}"

echo "==> docker buildx build (arm64) -> $IMAGE_URI"
docker buildx build \
  --platform linux/arm64 \
  --provenance=false \
  -t "$IMAGE_URI" \
  --push \
  "$ROOT_DIR"

echo "==> terraform apply (with image)"
terraform -chdir="$TF_DIR" apply -auto-approve -var "image_uri=$IMAGE_URI"

FN_NAME=$(terraform -chdir="$TF_DIR" output -raw lambda_function_name)
echo "==> deployed: $FN_NAME"
echo "    image: $IMAGE_URI"
echo "    manual invoke:"
echo "      aws lambda invoke --function-name $FN_NAME --profile $PROFILE --region $REGION /tmp/out.json && cat /tmp/out.json"
