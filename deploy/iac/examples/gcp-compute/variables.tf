# GCP-specific inputs. The NovaFabric inputs are passed straight through to the
# provider-neutral module, whose validation applies unchanged.

variable "project_id" {
  description = "Existing GCP project ID. No default."
  type        = string
}

variable "region" {
  description = "GCP region, e.g. europe-west8. No default: choosing a region is a cost and data-residency decision."
  type        = string
}

variable "zone" {
  description = "Zone in region, e.g. europe-west8-a."
  type        = string
}

variable "network" {
  description = "Existing VPC network name or self link. This example creates no network."
  type        = string
}

variable "subnetwork" {
  description = "Existing subnetwork name or self link in region."
  type        = string
}

variable "boot_image" {
  description = "Ubuntu 24.04 LTS image. The default is the public image family; pin an exact image (projects/ubuntu-os-cloud/global/images/<name>) for reproducible rebuilds."
  type        = string
  default     = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
}

variable "machine_type" {
  description = "Override the machine type. null maps the neutral size tier: small = e2-medium, medium = e2-standard-4, large = e2-standard-8."
  type        = string
  default     = null
}

variable "assign_public_ip" {
  description = "Attach an ephemeral external IPv4 address. Default false; external addresses are billed."
  type        = bool
  default     = false
}

variable "service_account_email" {
  description = "Existing service account to attach. Default null: no service account, so the VM has no Google Cloud identity at all."
  type        = string
  default     = null
}

variable "keep_disk_on_destroy" {
  description = "Keep the boot disk when the VM is destroyed. Default false: `tofu destroy` deletes the disk and every capsule on it. A kept disk keeps billing."
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
  description = "NAME of the secret holding the DSN (for example a Secret Manager secret ID). Never the DSN itself."
  type        = string
  default     = null
}

variable "workers" {
  description = "uvicorn workers; > 1 requires postgres."
  type        = number
  default     = 1
}

variable "admin_cidrs" {
  description = "CIDRs allowed to SSH. Default none (use OS Login through IAP instead)."
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
  description = "Boot disk size in GiB."
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
  description = "Extra labels."
  type        = map(string)
  default     = {}
}
