variable "region" {
  type    = string
  default = "ap-south-1"
}

variable "recording_bucket_name" {
  type        = string
  description = "Globally-unique S3 bucket for call recordings."
}

variable "recording_retention_days" {
  type    = number
  default = 30 # PRD §11
}

variable "db_instance_class" {
  type    = string
  default = "db.t3.medium"
}

variable "db_username" {
  type    = string
  default = "koel"
}

variable "db_password" {
  type      = string
  sensitive = true
}

variable "redis_node_type" {
  type    = string
  default = "cache.t3.small"
}

variable "redis_auth_token" {
  type      = string
  sensitive = true
}

# Network inputs — wire to an existing VPC module rather than creating one here.
variable "app_security_group_id" { type = string }
variable "db_subnet_group" { type = string }
variable "redis_subnet_group" { type = string }

# --- Compute (ECS Fargate) — the piece the AWS-residency deploy is missing:
# RDS/ElastiCache/S3/IAM above have nothing to run against them without this.
# Runs the SAME image + command as the live Render deployment (render.yaml):
# uvicorn koel.combined:app, one process serving every mounted sub-app.
variable "app_subnet_ids" {
  type        = list(string)
  description = "Private subnets for the Fargate service's ENIs (existing VPC — see app_security_group_id)."
}

variable "container_image_uri" {
  type        = string
  description = "Full ECR image URI (built from backend/Dockerfile) — e.g. <account>.dkr.ecr.ap-south-1.amazonaws.com/koel-backend:<tag>."
}

variable "container_port" {
  type    = number
  default = 8100
}

variable "task_cpu" {
  type        = string
  default     = "1024" # 1 vCPU
  description = "Fargate task-level CPU units (256/512/1024/2048/4096)."
}

variable "task_memory" {
  type        = string
  default     = "2048" # 2 GB — the combined process needs more than Render's free 512MB tier
  description = "Fargate task-level memory (MiB), must be a valid CPU/memory pairing."
}

variable "desired_count" {
  type    = number
  default = 1
}

variable "log_retention_days" {
  type    = number
  default = 30
}

# Existing Application Load Balancer target group (out of scope here, same as
# the VPC — wire to your existing ALB rather than provisioning a new one).
variable "alb_target_group_arn" {
  type        = string
  description = "Existing ALB target group the ECS service registers into."
}

# Runtime config the combined app needs (KOEL_DATABASE_URL, KOEL_REDIS_URL,
# secrets, provider keys, ...). Non-secret values only — secrets belong in
# AWS Secrets Manager / SSM Parameter Store and should be wired via the ECS
# task definition's `secrets` block in a real deployment, not this map.
variable "app_environment" {
  type    = map(string)
  default = {}
}
