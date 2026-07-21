# KOEL — production infrastructure (P/K). Deploy-gated artifact: review + `plan`
# before any `apply` — nothing here has been applied against a real AWS account.
# Region defaults to ap-south-1 to keep data in-region (PRD §11) — note the
# hosted-inference residency caveat in docs/RESIDENCY.md.
#
# Provisions: RDS Postgres (pgvector), ElastiCache Redis (Multi-AZ), the S3
# recordings bucket with a 30-day lifecycle expiry, least-privilege IAM (S3
# recordings write + Bedrock Titan Embeddings invoke), and an ECS Fargate
# service running the same image/command as the live Render deployment
# (render.yaml: `uvicorn koel.combined:app`) — the compute piece the other
# resources had nothing to run against before. VPC/subnets/SGs/ALB target
# group are referenced as inputs — wire to your existing network module
# rather than creating new ones here. SageMaker (Whisper/Orpheus, roadmap 6.8)
# is deliberately NOT provisioned — see the comment at the bottom of this file.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

provider "aws" {
  region = var.region
}

# --- Call recordings bucket + 30-day retention (K) --------------------------
resource "aws_s3_bucket" "recordings" {
  bucket = var.recording_bucket_name
  tags   = { app = "koel", data = "call-recordings", pii = "true" }
}

resource "aws_s3_bucket_public_access_block" "recordings" {
  bucket                  = aws_s3_bucket.recordings.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "recordings" {
  bucket = aws_s3_bucket.recordings.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "aws:kms" }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "recordings" {
  bucket = aws_s3_bucket.recordings.id
  rule {
    id     = "expire-recordings"
    status = "Enabled"
    filter { prefix = "recordings/" }
    expiration { days = var.recording_retention_days } # PRD §11: 30 days
  }
}

# --- RDS Postgres (pgvector) ------------------------------------------------
resource "aws_db_instance" "koel" {
  identifier                   = "koel-postgres"
  engine                       = "postgres"
  engine_version               = "16"
  instance_class               = var.db_instance_class
  allocated_storage            = 50
  storage_encrypted            = true
  db_name                      = "koel"
  username                     = var.db_username
  password                     = var.db_password # source from a secret manager
  multi_az                     = true
  backup_retention_period      = 7 # PITR ≥ 7 days
  deletion_protection          = true
  performance_insights_enabled = true
  vpc_security_group_ids       = [var.app_security_group_id]
  db_subnet_group_name         = var.db_subnet_group
  # pgvector: enable the `vector` extension via a parameter group / migration.
  tags = { app = "koel" }
}

# --- ElastiCache Redis (session store) --------------------------------------
resource "aws_elasticache_replication_group" "koel" {
  replication_group_id       = "koel-redis"
  description                = "KOEL session store (grace TTL, barge-in state)"
  engine                     = "redis"
  node_type                  = var.redis_node_type
  num_cache_clusters         = 2
  automatic_failover_enabled = true
  multi_az_enabled           = true
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true # rediss:// + AUTH (see docs/SECURITY.md)
  auth_token                 = var.redis_auth_token
  subnet_group_name          = var.redis_subnet_group
  security_group_ids         = [var.app_security_group_id]
  tags                       = { app = "koel" }
}

# --- IAM: app writes recordings, nothing more -------------------------------
data "aws_iam_policy_document" "recordings_write" {
  statement {
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["${aws_s3_bucket.recordings.arn}/recordings/*"]
  }
}

resource "aws_iam_policy" "recordings_write" {
  name   = "koel-recordings-write"
  policy = data.aws_iam_policy_document.recordings_write.json
}

# --- IAM: Bedrock invoke (Titan Embeddings v2, roadmap 6.8 / O-7 Track B) ---
# Scoped to the one model this codebase's BedrockTitanEmbedder actually calls
# (koel/nlp/rag.py) — widen the resource list if other Bedrock models are
# adopted later, don't grant "bedrock:*" up front.
data "aws_iam_policy_document" "bedrock_invoke" {
  statement {
    actions = ["bedrock:InvokeModel"]
    resources = [
      "arn:aws:bedrock:${var.region}::foundation-model/amazon.titan-embed-text-v2:0",
    ]
  }
}

resource "aws_iam_policy" "bedrock_invoke" {
  name   = "koel-bedrock-invoke"
  policy = data.aws_iam_policy_document.bedrock_invoke.json
}

# --- ECS Fargate: the compute the AWS-residency deploy was missing ---------
# Runs the same image + command as the live Render deployment (render.yaml):
# `uvicorn koel.combined:app`, one process serving every mounted sub-app.
resource "aws_ecs_cluster" "koel" {
  name = "koel"
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
  tags = { app = "koel" }
}

resource "aws_cloudwatch_log_group" "koel_app" {
  name              = "/ecs/koel-backend"
  retention_in_days = var.log_retention_days
  tags              = { app = "koel" }
}

# Execution role: what ECS itself needs (pull the image, write logs) — never
# the app's own AWS permissions (that's the task role below).
data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ecs_execution" {
  name               = "koel-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_execution_managed" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Task role: the APPLICATION's own permissions — recordings + Bedrock only,
# matching the least-privilege posture the recordings_write policy already set.
resource "aws_iam_role" "ecs_task" {
  name               = "koel-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_task_recordings" {
  role       = aws_iam_role.ecs_task.name
  policy_arn = aws_iam_policy.recordings_write.arn
}

resource "aws_iam_role_policy_attachment" "ecs_task_bedrock" {
  role       = aws_iam_role.ecs_task.name
  policy_arn = aws_iam_policy.bedrock_invoke.arn
}

resource "aws_ecs_task_definition" "koel_backend" {
  family                   = "koel-backend"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "koel-backend"
      image     = var.container_image_uri
      essential = true
      portMappings = [
        { containerPort = var.container_port, protocol = "tcp" }
      ]
      environment = [
        for k, v in var.app_environment : { name = k, value = v }
      ]
      # Secrets (KOEL_JWT_SECRET, KOEL_SECRET_KEY, provider API keys — see
      # docs/SECURITY.md) belong here as `secrets = [{ name = ..., valueFrom =
      # <Secrets Manager / SSM ARN> }]` in a real deployment, never in
      # `app_environment` (plain env vars land in `aws ecs describe-tasks`).
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.koel_app.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "koel"
        }
      }
    }
  ])

  tags = { app = "koel" }
}

resource "aws_ecs_service" "koel_backend" {
  name            = "koel-backend"
  cluster         = aws_ecs_cluster.koel.id
  task_definition = aws_ecs_task_definition.koel_backend.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.app_subnet_ids
    security_groups  = [var.app_security_group_id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = var.alb_target_group_arn
    container_name   = "koel-backend"
    container_port   = var.container_port
  }

  # koel/combined.py's healthCheckPath is /health (matches render.yaml) — the
  # ALB target group referenced above should already point health checks there.
  depends_on = [aws_iam_role_policy_attachment.ecs_execution_managed]

  tags = { app = "koel" }
}

# --- SageMaker (WhisperSageMakerProvider / OrpheusSageMakerProvider, roadmap
# 6.8) — deliberately NOT provisioned here. Unlike the resources above (which
# just need AWS API arguments), an aws_sagemaker_model needs a REAL trained
# model artifact (image_uri + model_data_url) — inventing placeholder values
# would be Terraform that looks complete but points at nothing, which is
# worse than an honest gap. Add aws_sagemaker_model / aws_sagemaker_endpoint_
# configuration / aws_sagemaker_endpoint resources here once a real Whisper/
# Orpheus model artifact has been packaged and pushed to S3/ECR.
