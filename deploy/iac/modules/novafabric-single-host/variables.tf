# Inputs of the provider-neutral single-host contract.
#
# Secure defaults: nothing is reachable from outside the host until an operator
# names CIDRs; the API binds loopback only; secrets are referenced by NAME and
# never accepted as values (so they never reach the state file through this
# module).

variable "name" {
  description = "Deployment name. Used for labels and by provider examples to name resources. Lowercase letters, digits and hyphens, starting with a letter, at most 32 characters."
  type        = string
  default     = "novafabric"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,31}$", var.name))
    error_message = "name must match ^[a-z][a-z0-9-]{0,31}$ (lowercase, starts with a letter, at most 32 characters)."
  }
}

variable "novafabric_version" {
  description = "Exact NovaFabric release to install from the package index, e.g. \"0.109.0\". Required and exact: a moving \"latest\" would make two applies of the same configuration install different software."
  type        = string

  validation {
    condition     = can(regex("^[0-9]+\\.[0-9]+\\.[0-9]+$", var.novafabric_version))
    error_message = "novafabric_version must be an exact X.Y.Z release (no ranges, no \"latest\")."
  }
}

variable "package_index_url" {
  description = "Optional Python package index (a PyPI mirror or an internal index for air-gapped sites). null uses pip's own default index. Must be https."
  type        = string
  default     = null

  validation {
    condition     = var.package_index_url == null || can(regex("^https://", var.package_index_url))
    error_message = "package_index_url must be an https:// URL (or null for pip's default index)."
  }
}

variable "backend" {
  description = "Server storage backend: \"sqlite\" (single host, no external service) or \"postgres\" (an EXISTING PostgreSQL service you operate; this module never provisions one)."
  type        = string
  default     = "sqlite"

  validation {
    condition     = contains(["sqlite", "postgres"], var.backend)
    error_message = "backend must be \"sqlite\" or \"postgres\"."
  }
}

variable "postgres_dsn_secret_ref" {
  description = "NAME (not value) of the secret that holds the PostgreSQL DSN in your secret store - for example an AWS Secrets Manager secret name or a GCP Secret Manager secret ID. Recorded on the host as a pointer only; the operator writes the DSN into /etc/novafabric/server.env. Required when backend = \"postgres\"."
  type        = string
  default     = null

  validation {
    # A pasted DSN (postgresql://user:password@host/db) is the exact mistake this
    # input exists to prevent: it would land in user-data and in the state file.
    condition     = var.postgres_dsn_secret_ref == null || !can(regex("(?i)^postgres(ql)?://|://[^/]*@", var.postgres_dsn_secret_ref))
    error_message = "postgres_dsn_secret_ref must be the NAME of a secret, not a DSN. Never put a connection string with a password in OpenTofu inputs."
  }
}

variable "workers" {
  description = "uvicorn worker processes for `nova server start --workers`. More than 1 requires backend = \"postgres\" (several processes cannot share one SQLite file)."
  type        = number
  default     = 1

  validation {
    condition     = var.workers >= 1 && var.workers <= 32 && floor(var.workers) == var.workers
    error_message = "workers must be a whole number between 1 and 32."
  }
}

variable "server_port" {
  description = "Loopback port the NovaFabric API listens on (127.0.0.1 only). It is never opened in the host firewall; the reverse proxy on the same host forwards to it."
  type        = number
  default     = 7433

  validation {
    condition     = var.server_port >= 1024 && var.server_port <= 65535 && !contains([22, 80, 443], var.server_port)
    error_message = "server_port must be an unprivileged port (1024-65535) and not 22, 80 or 443."
  }
}

variable "admin_cidrs" {
  description = "CIDRs allowed to reach SSH (tcp/22). Default: none - use your provider's console, session manager or identity-aware proxy instead. A match-everything range (prefix length 0, IPv4 or IPv6) is refused."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for c in var.admin_cidrs : can(cidrhost(c, 0))])
    error_message = "every admin_cidrs entry must be a valid CIDR, e.g. 203.0.113.0/24."
  }

  validation {
    condition     = alltrue([for c in var.admin_cidrs : !can(regex("/0$", c))])
    error_message = "admin_cidrs must not contain a /0 range: SSH is never opened to the whole internet."
  }
}

variable "client_cidrs" {
  description = "CIDRs allowed to reach the TLS reverse proxy (tcp/443). Default: none. A /0 range is refused unless allow_public_https = true."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for c in var.client_cidrs : can(cidrhost(c, 0))])
    error_message = "every client_cidrs entry must be a valid CIDR, e.g. 198.51.100.0/24."
  }
}

variable "allow_public_https" {
  description = "Explicit opt-in to accept a /0 range in client_cidrs (HTTPS from anywhere). The NovaFabric server is experimental and single-tenant; read docs/ops/server-admin-guide.md before setting this."
  type        = bool
  default     = false
}

variable "public_hostname" {
  description = "Hostname the operator's TLS certificate will cover. Used only to fill the reverse-proxy examples; null leaves the placeholder nova.example.com."
  type        = string
  default     = null

  validation {
    condition     = var.public_hostname == null || can(regex("^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$", var.public_hostname))
    error_message = "public_hostname must be a bare DNS name (no scheme, port or path)."
  }
}

variable "instance_size" {
  description = "Provider-neutral size tier: small (2 vCPU / 4 GiB), medium (4 vCPU / 16 GiB) or large (8 vCPU / 32 GiB). Unbenchmarked starting points for a pilot; see docs/ops/sizing-guide.md."
  type        = string
  default     = "small"

  validation {
    condition     = contains(["small", "medium", "large"], var.instance_size)
    error_message = "instance_size must be small, medium or large."
  }
}

variable "disk_gib" {
  description = "Boot/data disk size in GiB. Capsules, the SQLite registry and the server token live on it under /var/lib/novafabric."
  type        = number
  default     = 50

  validation {
    condition     = var.disk_gib >= 20 && var.disk_gib <= 16384
    error_message = "disk_gib must be between 20 and 16384."
  }
}

variable "oidc_issuer_url" {
  description = "Optional OIDC issuer (https). Set together with oidc_audience; written to the oidc: block of server.yaml. Neither value is a secret."
  type        = string
  default     = null

  validation {
    condition     = var.oidc_issuer_url == null || can(regex("^https://", var.oidc_issuer_url))
    error_message = "oidc_issuer_url must be an https:// URL."
  }
}

variable "oidc_audience" {
  description = "Optional OIDC audience. Set together with oidc_issuer_url."
  type        = string
  default     = null
}

variable "labels" {
  description = "Extra labels/tags merged over the defaults (app, managed-by, novafabric-version). Keys and values: lowercase letters, digits, '_' and '-' (the strictest provider rule, GCP's)."
  type        = map(string)
  default     = {}

  validation {
    condition = alltrue([
      for k, v in var.labels :
      can(regex("^[a-z][a-z0-9_-]{0,62}$", k)) && can(regex("^[a-z0-9_-]{0,63}$", v))
    ])
    error_message = "label keys must match ^[a-z][a-z0-9_-]{0,62}$ and values ^[a-z0-9_-]{0,63}$."
  }
}
