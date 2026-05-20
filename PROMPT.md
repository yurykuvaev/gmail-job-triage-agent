# Job Search Email Triage Agent — Project Spec

Build a serverless agent that reads my Gmail for the last 14 days, classifies job-search emails using Claude, and sends a daily summary to Telegram. Deploy on AWS via Terraform.

## My context
- DevOps/Platform Engineer, comfortable with Terraform, AWS, Python
- Actively job searching (Senior DevOps/SRE roles)
- Want this to also work as a portfolio piece — clean code, good README, infra-as-code

## Architecture
```
EventBridge (cron 12:00 UTC = 8am Miami)
    -> Lambda (Python 3.12, container image)
        -> Gmail API (read-only, last 14 days first run, then last 24h)
        -> Anthropic API (Claude Sonnet 4.6 — model id: claude-sonnet-4-6)
        -> Telegram Bot API (sendMessage)
    Secrets in AWS Secrets Manager
    State (processed message IDs) in DynamoDB
```

## Functional requirements

### Email classification
For each email in the lookback window, classify into:
- `interview_invite` — recruiter inviting to interview, scheduling links
- `rejection` — explicit "we decided not to move forward"
- `recruiter_outreach` — cold/warm recruiter pitch for a new role
- `application_received` — auto-confirmation from ATS
- `followup_needed` — assessment/take-home, deadline, action required from me
- `other` — newsletters, unrelated

Extract per email: `company`, `role`, `next_step`, `deadline` (ISO date or null), `link` (most relevant URL).

### Summary format (Telegram message)
Markdown-formatted. Group by category. Telegram has 4096 char limit per message — split if needed.

```
Job Search Summary — <date>
Scanned <N> emails over last <window>

Interviews (<count>)
- <Company> — <role> — <next_step> — <deadline>

Rejections (<count>)
- <Company> — <role>

Recruiter outreach (<count>)
- <Company> — <role> — <link>

Action needed (<count>)
- <Company> — <next_step> — <deadline>

Applications confirmed (<count>)
- <Company> — <role>
```

Skip empty sections. If `other` > 0, just say "N other emails skipped."

### State management
- First run: 14-day lookback
- Subsequent runs: 24-hour lookback
- DynamoDB table `email-agent-state` with PK `message_id` (string), TTL 30 days
- Skip already-processed message_ids to avoid duplicate summaries

(See README.md for the full setup and deployment walkthrough. This file preserves the original brief.)
