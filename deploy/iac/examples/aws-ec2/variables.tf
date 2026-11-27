# AWS-specific inputs. The NovaFabric inputs are passed straight through to the
# provider-neutral module, whose validation applies unchanged.

variable "region" {
  description = "AWS region, e.g. eu-south-1. No default: choosing a region is a cost and data-residency decision."
  type        = string
}

variable "vpc_id" {
  description = "Existing VPC to place the instance in. This example creates no network."
  type        = string
}

variable "subnet_id" {
  description = "Existing subnet in vpc_id. A private subnet is the expected default."
  type        = string
}

variable "ami_id" {
  description = "Exact Ubuntu 24.04 LTS AMI ID for the region (operator-pinned; no image lookup is made)."
  type        = string

  validation {
    condition     = can(regex("^ami-[0-9a-f]{8,17}$", var.ami_id))
    error_message = "ami_id must be an AMI ID such as ami-0123456789abcdef0."
  }
}

variable "instance_type" {
  description = "Override the EC2 type. null maps the neutral size tier: small = t3.medium, medium = m7i.xlarge, large = m7i.2xlarge."
  type        = string
  default     = null
}

variable "associate_public_ip_address" {
  description = "Give the instance a public IPv4 address. Default false; public IPv4 addresses are billed."
  type        = bool
  default     = false
}

variable "key_name" {
  description = "Existing EC2 key pair for SSH. Default null (no key: use Session Manager with an instance profile you control)."
  type        = string
  default     = null
}

variable "iam_instance_profile" {
  description = "Existing instance profile NAME. Default null: the instance gets no AWS identity at all, the least privilege there is."
  type        = string
  default     = null
}

variable "egress_cidrs" {
  description = "Destinations for outbound tcp/80 and tcp/443 (OS packages and the Python package index). Narrow this to your mirror or proxy if you have one."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "postgres_cidrs" {
  description = "CIDRs of the EXISTING PostgreSQL service (outbound tcp/5432). Only used with backend = \"postgres\"."
  type        = list(string)
  default     = []
}

variable "keep_disk_on_destroy" {
  description = "Keep the root EBS volume when the instance is destroyed. Default false: `tofu destroy` deletes the disk and every capsule on it. A kept volume keeps billing."
  type        = bool
  default     = false
}

# ---- passed through to the provider-neutral module -------------------------

variable "name" {
  description = "Deployment name (see the module)."
  type        = string
  default     = "novafabric"
}

variable "novafabric_version" {
  description = "Exact NovaFabric release, e.g. \"0.109.0\"."
  type        = string
}

variable "package_index_url" {
  description = "Optional https package index (mirror)."
  type        = string
  default     = null
}

variable "backend" {
  description = "\"sqlite\" or \"postgres\" (existing service)."
  type        = string
  default     = "sqlite"
}

variable "postgres_dsn_secret_ref" {
  description = "NAME of the secret holding the DSN (for example a Secrets Manager secret name). Never the DSN itself."
  type        = string
  default     = null
}

variable "workers" {
  description = "uvicorn workers; > 1 requires postgres."
  type        = number
  default     = 1
}

variable "admin_cidrs" {
  description = "CIDRs allowed to SSH. Default none."
  type        = list(string)
  default     = []
}

variable "client_cidrs" {
  description = "CIDRs allowed to reach the TLS reverse proxy on tcp/443. Default none."
  type        = list(string)
  default     = []
}

variable "allow_public_https" {
  description = "Explicit opt-in to accept a /0 range in client_cidrs."
  type        = bool
  default     = false
}

variable "public_hostname" {
  description = "Hostname for the reverse-proxy examples."
  type        = string
  default     = null
}

variable "instance_size" {
  description = "small, medium or large."
  type        = string
  default     = "small"
}

variable "disk_gib" {
  description = "Root disk size in GiB."
  type        = number
  default     = 50
}

variable "oidc_issuer_url" {
  description = "Optional OIDC issuer (https)."
  type        = string
  default     = null
}

variable "oidc_audience" {
  description = "Optional OIDC audience."
  type        = string
  default     = null
}

variable "labels" {
  description = "Extra tags."
  type        = map(string)
  default     = {}
}
