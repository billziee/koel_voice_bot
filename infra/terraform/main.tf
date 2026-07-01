# KOEL — production infrastructure (P/K). Deploy-gated artifact: review + `plan`
# before any `apply`. Region defaults to ap-south-1 to keep data in-region
# (PRD §11) — note the hosted-inference residency caveat in docs/RESIDENCY.md.
#
# Provisions: RDS Postgres (pgvector), ElastiCache Redis (Multi-AZ), the S3
# recordings bucket with a 30-day lifecycle expiry, and least-privilege IAM for
# the app to write recordings. VPC/subnets/SGs are referenced as inputs — wire to
# your existing network module rather than creating a new VPC here.

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
  tags                         = { app = "koel" }
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
