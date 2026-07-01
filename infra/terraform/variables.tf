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
