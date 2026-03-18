# Fargate Deployment Prerequisites

Before the GitHub Actions deploy pipeline can run, create these AWS resources once.

## 1. ECR Repositories

```bash
aws ecr create-repository --repository-name nl-query-tool-backend --region us-east-1
aws ecr create-repository --repository-name nl-query-tool-frontend --region us-east-1
```

## 2. S3 Buckets

```bash
aws s3 mb s3://nl-query-tool-uploads-<YOUR_ACCOUNT_ID>  --region us-east-1
aws s3 mb s3://nl-query-tool-schemas-<YOUR_ACCOUNT_ID>  --region us-east-1
```

## 3. Secrets Manager Entries

```bash
# OpenAI API key
aws secretsmanager create-secret \
  --name "nl-query-tool/openai-api-key" \
  --secret-string "sk-..."

# Fernet key for connection credential encryption
# Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
aws secretsmanager create-secret \
  --name "nl-query-tool/secret-store-key" \
  --secret-string "<GENERATED_FERNET_KEY>"
```

## 4. IAM Roles

### ECS Task Role (nl-query-tool-task-role)
The role assumed by running containers. Needs:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "athena:StartQueryExecution",
        "athena:GetQueryExecution",
        "athena:GetQueryResults",
        "athena:StopQueryExecution",
        "athena:ListTableMetadata",
        "athena:GetTableMetadata"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": ["glue:GetDatabase", "glue:GetDatabases", "glue:GetTable", "glue:GetTables"],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket", "s3:GetBucketLocation"],
      "Resource": [
        "arn:aws:s3:::nl-query-tool-uploads-*",
        "arn:aws:s3:::nl-query-tool-uploads-*/*",
        "arn:aws:s3:::nl-query-tool-schemas-*",
        "arn:aws:s3:::nl-query-tool-schemas-*/*",
        "arn:aws:s3:::<YOUR_ATHENA_STAGING_BUCKET>/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue",
        "secretsmanager:PutSecretValue",
        "secretsmanager:CreateSecret",
        "secretsmanager:DeleteSecret",
        "secretsmanager:DescribeSecret"
      ],
      "Resource": "arn:aws:secretsmanager:*:*:secret:nl-query-tool/*"
    }
  ]
}
```

### ECS Execution Role (nl-query-tool-execution-role)
Used by ECS to pull images and inject secrets. Attach the AWS managed policy:
- `AmazonECSTaskExecutionRolePolicy`

Plus a custom policy to read the Secrets Manager entries:
```json
{
  "Effect": "Allow",
  "Action": ["secretsmanager:GetSecretValue"],
  "Resource": [
    "arn:aws:secretsmanager:*:*:secret:nl-query-tool/openai-api-key*",
    "arn:aws:secretsmanager:*:*:secret:nl-query-tool/secret-store-key*"
  ]
}
```

### GitHub Actions Deploy Role (nl-query-tool-github-deploy)
Trusted by the GitHub OIDC provider. Needs:
- `AmazonEC2ContainerRegistryPowerUser`
- `AmazonECS_FullAccess` (or scoped to the specific cluster/services)

## 5. CloudWatch Log Groups

```bash
aws logs create-log-group --log-group-name /ecs/nl-query-tool-backend  --region us-east-1
aws logs create-log-group --log-group-name /ecs/nl-query-tool-frontend --region us-east-1
```

## 6. GitHub Repository Secrets

Add these under `Settings → Secrets and variables → Actions`:

| Secret | Value |
|--------|-------|
| `AWS_REGION` | `us-east-1` |
| `AWS_ROLE_ARN` | ARN of the GitHub deploy role |
| `ECR_BACKEND_URL` | `<account>.dkr.ecr.us-east-1.amazonaws.com/nl-query-tool-backend` |
| `ECR_FRONTEND_URL` | `<account>.dkr.ecr.us-east-1.amazonaws.com/nl-query-tool-frontend` |
| `ECS_CLUSTER_NAME` | Name of your ECS cluster |
| `ECS_BACKEND_SERVICE` | Name of the backend ECS service |
| `ECS_FRONTEND_SERVICE` | Name of the frontend ECS service |
| `ECS_TASK_ROLE_ARN` | ARN of the task role |
| `ECS_EXECUTION_ROLE_ARN` | ARN of the execution role |
| `OPENAI_API_KEY` | For CI golden tests (not Fargate — Fargate uses Secrets Manager) |
| `OPENAI_API_KEY_ARN` | Secrets Manager ARN for `nl-query-tool/openai-api-key` |
| `SECRET_STORE_KEY_ARN` | Secrets Manager ARN for `nl-query-tool/secret-store-key` |
| `UPLOAD_BUCKET` | `nl-query-tool-uploads-<account>` |
| `SCHEMA_BUCKET` | `nl-query-tool-schemas-<account>` |
| `ALB_DNS_NAME` | DNS name of the Application Load Balancer |
