# gmail-job-triage-agent

Serverless agent that reads my Gmail, classifies job-search emails with Claude Sonnet 4.6, and posts a daily summary to Telegram. Deployed to AWS with Terraform.

```
EventBridge (cron 12:00 UTC) -> Lambda (Python 3.12 arm64 container)
                                  -> Gmail API (read-only)
                                  -> Anthropic API (claude-sonnet-4-6)
                                  -> Telegram Bot API
                                Secrets: SSM Parameter Store (SecureString)
                                State:   DynamoDB (TTL 30d)
```

## Why this exists

I'm actively interviewing for Senior DevOps/SRE roles. My inbox is loud — recruiter cold mail, ATS confirmations, the occasional real interview invite. This agent triages all of it daily so I can act on the ~3 emails that matter without scrolling through the ~30 that don't.

## Why these choices

- **Lambda container, not zip** — Anthropic + Google + boto3 + httpx easily blow past the 250 MB zip limit; containers also remove the "did my layer rebuild?" guesswork.
- **arm64 (Graviton)** — ~20% cheaper, fully supported by every dep we use.
- **`pyproject.toml` over `requirements.txt`** — single source of truth for metadata + deps + ruff config; pip and uv both read it.
- **One Anthropic call, batched** — round-trip cost dominates token cost at this volume.
- **DynamoDB on-demand** — < 1 KB per dedup record, on-demand is free at this scale and removes capacity planning.
- **SSM Parameter Store SecureString over Secrets Manager** — same KMS-encrypted storage but free for Standard tier (≤ 4 KB, ≤ 10k params/account). One credential per parameter means rotation is one CLI command, not read-modify-write of a JSON blob.
- **Terraform S3 backend (`tf-state-yury`, key `gmail-job-triage-agent/`)** — matches my standing convention across all personal projects.

---

## Setup

### 1. Google Cloud Console (one-time)

1. https://console.cloud.google.com → **Select project** → **New project**, name it `gmail-triage` (or reuse an existing project).
2. **APIs & Services → Library** → search "Gmail API" → **Enable**.
3. **APIs & Services → OAuth consent screen**:
   - User Type: **External**.
   - App name: `Gmail Triage Agent`. User support email + developer email: your address.
   - **Scopes**: add `.../auth/gmail.readonly`.
   - **Test users**: add your own Gmail address. (Keep the app in *Testing* status — no review needed; refresh tokens for test users last 7 days, so for long-lived access either publish the app or accept the periodic re-auth.)
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Desktop app**.
   - Name: `gmail-triage-desktop`.
   - **Download JSON** → save somewhere outside this repo (e.g. `~/secrets/oauth_client.json`). Never commit this.

### 2. Generate a Gmail refresh token locally

```powershell
cd gmail-job-triage-agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
python scripts\gmail_oauth_local.py C:\path\to\oauth_client.json
```

A browser opens. Sign in with the same Gmail address you added as a test user. The script prints `gmail_client_id`, `gmail_client_secret`, `gmail_refresh_token` — keep them; they go into three of the six SSM SecureString parameters (see §6).

### 3. Telegram bot

1. In Telegram, message [@BotFather](https://t.me/BotFather) → `/newbot` → follow the prompts → copy the `bot_token`.
2. Get your `chat_id`:
   - Send any message to your new bot first.
   - `curl "https://api.telegram.org/bot<TOKEN>/getUpdates"` → look for `"chat":{"id":...}`.

### 4. AWS prerequisites

- `~/.aws/credentials` with profile `k8s-lab` (or override `AWS_PROFILE`).
- S3 bucket `tf-state-yury` already exists (personal convention).
- Docker Desktop running (buildx required to cross-build arm64 from x86 hosts).

### 5. Deploy

```powershell
cd terraform
Copy-Item terraform.tfvars.example terraform.tfvars
# edit if needed — defaults are sensible

cd ..
bash scripts/deploy.sh        # WSL or Git Bash; PowerShell users: run via `wsl bash ...`
```

`deploy.sh` does:

1. `terraform apply` with `image_uri=""` — creates ECR, six SSM SecureString parameters (placeholders), DynamoDB, IAM, log group, EventBridge rule.
2. `docker buildx --platform linux/arm64 --push` to ECR.
3. `terraform apply` again with the real image URI — creates the Lambda function and EventBridge target.

### 6. Populate the credentials (SSM Parameter Store SecureString)

Credentials live in six separate SecureString parameters under `/email-agent/*`. Standard-tier SecureString is free and encrypted with the AWS-managed `alias/aws/ssm` KMS key.

Populate them once (one command per value):

```powershell
$path = terraform -chdir=terraform output -raw ssm_param_path

aws ssm put-parameter --name "$path/anthropic_api_key"   --type SecureString --overwrite --value "sk-ant-..."                           --profile k8s-lab --region us-east-1
aws ssm put-parameter --name "$path/gmail_client_id"     --type SecureString --overwrite --value "...apps.googleusercontent.com"        --profile k8s-lab --region us-east-1
aws ssm put-parameter --name "$path/gmail_client_secret" --type SecureString --overwrite --value "GOCSPX-..."                           --profile k8s-lab --region us-east-1
aws ssm put-parameter --name "$path/gmail_refresh_token" --type SecureString --overwrite --value "1//..."                               --profile k8s-lab --region us-east-1
aws ssm put-parameter --name "$path/telegram_bot_token"  --type SecureString --overwrite --value "12345:ABC..."                         --profile k8s-lab --region us-east-1
aws ssm put-parameter --name "$path/telegram_chat_id"    --type SecureString --overwrite --value "987654321"                            --profile k8s-lab --region us-east-1
```

The Terraform `value` attribute has `ignore_changes` set, so this won't fight with subsequent `terraform apply`.

**Rotating one value later** (e.g. the weekly Gmail refresh token while the OAuth consent screen stays in Testing mode):

```powershell
$path = terraform -chdir=terraform output -raw ssm_param_path
aws ssm put-parameter --name "$path/gmail_refresh_token" `
  --type SecureString --overwrite --value "1//NEW_TOKEN" `
  --profile k8s-lab --region us-east-1
```

One command, no read-modify-write, no JSON wrangling. The Lambda re-reads all six on every invoke, so the next run picks up the new value with no redeploy.

**Verify what's stored** (without printing secret values — only the names):

```powershell
terraform -chdir=terraform output -json ssm_param_names | ConvertFrom-Json
```

### 7. First invoke (14-day lookback)

The Lambda picks 14 days vs. 24 hours by checking whether the DynamoDB state table is empty. So the very first invoke automatically scans the last 14 days.

```powershell
$fn = terraform -chdir=terraform output -raw lambda_function_name
aws lambda invoke `
  --function-name $fn `
  --profile k8s-lab --region us-east-1 `
  C:\temp\out.json
Get-Content C:\temp\out.json
```

Telegram should ping within a few seconds. Subsequent runs (scheduled or manual) will fall back to the 24-hour window.

### 8. Reading logs

```powershell
aws logs tail /aws/lambda/email-agent --follow --profile k8s-lab --region us-east-1
```

Every run emits structured JSON with: `emails_fetched`, `emails_classified`, `emails_skipped_dedup`, `tokens_input`, `tokens_output`, `telegram_sent`, `duration_ms`.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `invalid_grant` on Gmail refresh | Refresh token expired (OAuth consent screen still in *Testing* status — tokens last 7 days). Re-run `scripts/gmail_oauth_local.py`. |
| Telegram: `chat not found` | You haven't sent at least one message to the bot yet, so the bot can't initiate the chat. |
| Lambda timeout | First 14-day run on a busy inbox can be slow. Bump `timeout` in Terraform (still under the 900s ceiling) or reduce `max_results` in `gmail_client.py`. |
| Classifier JSON parse failures | One retry with a stricter reminder is built in. If you see repeated failures in CloudWatch, capture the offending response and tighten `SYSTEM_PROMPT`. |
| `terraform apply` fails on `image_uri` | Run `scripts/deploy.sh` instead — it does the two-phase apply for you. |

---

## Cost guardrails

- **Token cap**: if the combined prompt for one run would exceed ~200k tokens, the classifier processes only the first 30 emails and logs a warning.
- **Retry budget**: 3 attempts max on the Anthropic call, exponential backoff.
- **ECR lifecycle**: keeps only the last 5 images.
- **CloudWatch retention**: 14 days.
- **DynamoDB**: pay-per-request, ~1 KB per record, TTL 30 days. Effectively free at < 100 emails/day.

Rough monthly cost at ~30 emails/day: Lambda < $0.05, DynamoDB < $0.05, SSM Parameter Store (Standard SecureString) **free**, ECR storage $0.01, EventBridge free. Anthropic API is the dominant cost — call it ~$0.50/month for one batched Sonnet 4.6 call per day. (Switched from Secrets Manager to SSM Parameter Store specifically to drop the $0.40/secret/month baseline.)

---

## Future work (not built)

- Auto-add interview invites to Google Calendar.
- Pipeline tracker: which company at which stage, weekly digest, follow-up reminders.
- Draft reply suggestions for recruiter outreach (could land as Gmail drafts via the same OAuth scope upgraded to `gmail.compose`).
- Slack alternative to Telegram (the `telegram_client.py` interface is narrow enough to swap).
- SNS topic + email subscription for the existing Lambda errors alarm.
