# CPDCL Bill Fetcher — Lambda Deployment Guide

Deploys the CPDCL bill screenshot tool as a container-based AWS Lambda that
runs automatically on the 1st of every month. No Bedrock or paid AI APIs are
used — CAPTCHA solving is handled by Tesseract OCR (free, runs inside the
container).

---

## Architecture

```
EventBridge (monthly cron)
        │
        ▼
   Lambda (container image, 3008 MB, 10 min timeout)
        │  • Playwright + headless Chromium
        │  • Tesseract OCR (CAPTCHA)
        │  • Pillow (image collation)
        ▼
   S3 bucket
     bills/YYYY-MM/receipt_all.png   ← collated A4 print page
     bills/YYYY-MM/bill_<acct>.png   ← individual bill screenshots
```

---

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| AWS CLI | v2 | https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html |
| Docker | 20+ | https://docs.docker.com/get-docker/ |
| AWS account | — | with IAM permissions to create Lambda, ECR, S3, EventBridge, IAM roles |

Configure your AWS credentials:
```bash
aws configure
# Enter: Access Key, Secret Key, region (e.g. us-east-1), output format (json)
```

Confirm your account ID — you'll need it throughout:
```bash
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export AWS_REGION=us-east-1        # change if deploying elsewhere
echo "Account: $AWS_ACCOUNT_ID  Region: $AWS_REGION"
```

---

## Step 1 — Verify Email in SES

The Lambda emails the collated bill image after every run. Both sender and
recipient must be verified while SES is in sandbox mode.

```bash
aws ses verify-email-identity --email-address sas3d@yahoo.com --region us-east-1
```

Check your inbox for the verification link from AWS and click it. To confirm:
```bash
aws ses get-identity-verification-attributes --identities sas3d@yahoo.com --region us-east-1
# "VerificationStatus": "Success"  ← you're good to go
```

> To send to *any* address (not just verified ones), request SES production
> access: AWS Console → SES → Account dashboard → Request production access.

---

## Step 2 — Create S3 Bucket  

```bash
export S3_BUCKET=bills-vja-$(echo $AWS_ACCOUNT_ID | tail -c 5)   # unique suffix
aws s3 mb s3://$S3_BUCKET --region $AWS_REGION
```

Optional: add a lifecycle rule to move older files to cheaper storage:
```bash
aws s3api put-bucket-lifecycle-configuration \
  --bucket $S3_BUCKET \
  --lifecycle-configuration '{
    "Rules": [{
      "ID": "archive-old-bills",
      "Status": "Enabled",
      "Filter": {"Prefix": "bills/"},
      "Transitions": [{"Days": 90, "StorageClass": "STANDARD_IA"}]
    }]
  }'
```

---

## Step 3 — Create IAM Role for Lambda

```bash
# Trust policy
cat > /tmp/trust.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "lambda.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
EOF

aws iam create-role \
  --role-name bills-vja-lambda-role \
  --assume-role-policy-document file:///tmp/trust.json

# Permission policy (replace YOUR-BUCKET-NAME)
cat > /tmp/policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3Write",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject"],
      "Resource": "arn:aws:s3:::${S3_BUCKET}/bills/*"
    },
    {
      "Sid": "SesSend",
      "Effect": "Allow",
      "Action": "ses:SendRawEmail",
      "Resource": "*"
    },
    {
      "Sid": "Logs",
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:*:*:log-group:/aws/lambda/bills-vja:*"
    }
  ]
}
EOF

aws iam put-role-policy \
  --role-name bills-vja-lambda-role \
  --policy-name bills-vja-policy \
  --policy-document file:///tmp/policy.json
```

> **No Bedrock permissions needed** — CAPTCHA is solved with Tesseract OCR
> running inside the container. Zero AI API costs.

---

## Step 3 — Create ECR Repository & Push Image

```bash
# Create repository
aws ecr create-repository \
  --repository-name bills-vja \
  --region $AWS_REGION

# Authenticate Docker to ECR
aws ecr get-login-password --region $AWS_REGION \
  | docker login --username AWS --password-stdin \
    $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com

# Build image (run from repo root)
docker build -t bills-vja ./lambda

# Tag and push
docker tag bills-vja:latest \
  $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/bills-vja:latest

docker push \
  $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/bills-vja:latest
```

> The first build takes ~10 minutes (downloads Chromium ~170 MB).
> Subsequent builds reuse cached layers and take ~1 minute.

---

## Step 4 — Create Lambda Function

```bash
aws lambda create-function \
  --function-name bills-vja \
  --package-type Image \
  --code ImageUri=$AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/bills-vja:latest \
  --role arn:aws:iam::$AWS_ACCOUNT_ID:role/bills-vja-lambda-role \
  --timeout 600 \
  --memory-size 3008 \
  --region $AWS_REGION \
  --environment "Variables={S3_BUCKET=$S3_BUCKET,EMAIL_TO=sas3d@yahoo.com}"
```

### Optional environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `S3_BUCKET` | *(required)* | S3 bucket name |
| `EMAIL_TO` | *(required)* | Recipient email address (must be SES-verified) |
| `EMAIL_FROM` | same as `EMAIL_TO` | Sender address if different |
| `S3_PREFIX` | `bills` | Key prefix inside the bucket |
| `ACCOUNT_NUMBERS` | hardcoded 3 accounts | Comma-separated list to override |

To add or update account numbers later:
```bash
aws lambda update-function-configuration \
  --function-name bills-vja \
  --environment "Variables={S3_BUCKET=$S3_BUCKET,ACCOUNT_NUMBERS=6423244002992,6423244145358,6423244217704}"
```

---

## Step 5 — Schedule Monthly with EventBridge

Runs at 09:00 UTC on the 1st of every month:

```bash
# Create the rule
aws events put-rule \
  --name bills-vja-monthly \
  --schedule-expression "cron(0 9 1 * ? *)" \
  --state ENABLED \
  --region $AWS_REGION

# Allow EventBridge to invoke the Lambda
aws lambda add-permission \
  --function-name bills-vja \
  --statement-id EventBridgeMonthly \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn arn:aws:events:$AWS_REGION:$AWS_ACCOUNT_ID:rule/bills-vja-monthly \
  --region $AWS_REGION

# Wire the rule to the Lambda
aws events put-targets \
  --rule bills-vja-monthly \
  --region $AWS_REGION \
  --targets "Id=1,Arn=arn:aws:lambda:$AWS_REGION:$AWS_ACCOUNT_ID:function:bills-vja"
```

---

## Step 6 — Test Manually

```bash
aws lambda invoke \
  --function-name bills-vja \
  --payload '{}' \
  --cli-binary-format raw-in-base64-out \
  --region $AWS_REGION \
  /tmp/response.json

cat /tmp/response.json
```

Expected response:
```json
{
  "statusCode": 200,
  "body": "{\"date\": \"2026-04\", \"succeeded\": [\"6423244002992\", \"6423244145358\", \"6423244217704\"], \"failed\": [], \"s3_keys\": [\"bills/2026-04/receipt_all.png\", ...]}"
}
```

Download the collated receipt:
```bash
aws s3 cp s3://$S3_BUCKET/bills/$(date +%Y-%m)/receipt_all.png ~/receipt_all.png
open ~/receipt_all.png   # macOS; use xdg-open on Linux
```

---

## Step 7 — Update Image (Future Deploys)

```bash
docker build -t bills-vja ./lambda
docker tag bills-vja:latest $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/bills-vja:latest
docker push $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/bills-vja:latest

aws lambda update-function-code \
  --function-name bills-vja \
  --image-uri $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/bills-vja:latest \
  --region $AWS_REGION
```

---

## Cost Estimate (per monthly run)

All prices are AWS us-east-1 as of 2026. **Actual bill will be near $0.**

| Service | Usage per run | Unit price | Cost |
|---------|--------------|-----------|------|
| **Lambda compute** | ~3 min × 3008 MB = ~540,000 GB-s | $0.0000166667/GB-s | ~$0.009 |
| **Lambda requests** | 1 invocation | $0.20/1M | ~$0.00000020 |
| **S3 PUT** | 4 objects (3 bills + 1 collated) | $0.005/1000 | ~$0.00002 |
| **S3 storage** | ~15 MB/month | $0.023/GB | ~$0.00035 |
| **ECR storage** | ~1.5 GB image | $0.10/GB/month | ~$0.15 |
| **EventBridge** | 1 scheduled event/month | Free (first 14M/month free) | $0.00 |
| **Data transfer** | ~5 MB outbound (portal pages) | Free (first 100 GB/month) | $0.00 |
| **Bedrock / AI API** | 0 — uses Tesseract OCR | — | **$0.00** |

**Total per run: ~$0.16/month** (almost entirely ECR image storage)

> To eliminate ECR cost (~$0.15), you can delete and re-push the image just
> before each run, keeping the stored image size near zero between runs.
> In practice $0.16/month rounds to free.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `error while loading shared libraries: libnss3.so` | Missing OS dependency | Ensure `nss` is in the `dnf install` list in Dockerfile |
| `Target closed` or browser crash | Missing `--disable-dev-shm-usage` or `--single-process` | Verify `CHROMIUM_ARGS` in `lambda_function.py` |
| `Failed to fetch` after 4 retries | Tesseract misread captcha too many times | Increase `max_retries` or check Tesseract logs |
| `NoSuchBucket` | S3 bucket doesn't exist or wrong region | Re-check `S3_BUCKET` env var and bucket region |
| Lambda timeout | Portal slow or Chromium startup slow | Cold starts can take 30–40s; 600s timeout is sufficient |
| `No space left on device` | `/tmp` full | Unlikely (<20 MB needed); check for leftover files from prior warm invocation — code overwrites files by path so this shouldn't occur |

---

## Cleanup (Remove All Resources)

```bash
aws lambda delete-function --function-name bills-vja --region $AWS_REGION
aws events remove-targets --rule bills-vja-monthly --ids 1 --region $AWS_REGION
aws events delete-rule --name bills-vja-monthly --region $AWS_REGION
aws ecr delete-repository --repository-name bills-vja --force --region $AWS_REGION
aws iam delete-role-policy --role-name bills-vja-lambda-role --policy-name bills-vja-policy
aws iam delete-role --role-name bills-vja-lambda-role
aws s3 rm s3://$S3_BUCKET --recursive
aws s3 rb s3://$S3_BUCKET
```
