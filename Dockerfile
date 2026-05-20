# Lambda container image for the email triage agent.
# Base: AWS-maintained Python 3.12 runtime on arm64 (cheaper Graviton billing).
FROM public.ecr.aws/lambda/python:3.12-arm64

# pyproject + lockless install: copy metadata first so deps cache survives source edits.
COPY pyproject.toml ${LAMBDA_TASK_ROOT}/

# pip can resolve from pyproject [project.dependencies] via PEP 621.
RUN pip install --no-cache-dir \
        "anthropic>=0.40.0" \
        "google-auth>=2.35.0" \
        "google-auth-oauthlib>=1.2.1" \
        "google-api-python-client>=2.149.0" \
        "boto3>=1.35.0" \
        "httpx>=0.27.0" \
        "pydantic>=2.9.0"

COPY src/ ${LAMBDA_TASK_ROOT}/src/

CMD ["src.handler.lambda_handler"]
