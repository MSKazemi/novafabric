# Rendering only: every value below is computed from the inputs, nothing is
# created. The cloud-init document is built as an HCL object and serialised
# with yamlencode(), so indentation in embedded files cannot break the YAML.

locals {
  service_user = "novafabric"
  state_dir    = "/var/lib/novafabric"
  config_dir   = "/etc/novafabric"
  venv_dir     = "/opt/novafabric/venv"
  env_file     = "/etc/novafabric/server.env"
  listen_host  = "127.0.0.1"
  hostname     = coalesce(var.public_hostname, "nova.example.com")

  # Provider-neutral size tiers. Unbenchmarked pilot starting points; provider
  # examples map each tier to a concrete instance or machine type.
  size_tiers = {
    small  = { vcpus = 2, memory_gib = 4 }
    medium = { vcpus = 4, memory_gib = 16 }
    large  = { vcpus = 8, memory_gib = 32 }
  }

  oidc_enabled = var.oidc_issuer_url != null && var.oidc_audience != null

  # server.yaml holds NO secrets: the server drops postgres_dsn, scim_token,
  # offline_key_path and local_token from YAML on load and reads them from the
  # environment instead.
  # (Filtered for-expressions instead of `cond ? {..} : {}`, whose two object
  # types differ and are rejected as inconsistent conditional results.)
  server_config = merge(
    {
      backend = var.backend
      host    = local.listen_host
      port    = var.server_port
    },
    {
      for k, v in {
        oidc = {
          issuer_url = var.oidc_issuer_url
          audience   = var.oidc_audience
        }
      } : k => v if local.oidc_enabled
    },
  )

  # Secret NAMES, mapped to the environment variable each one must populate.
  secret_references = {
    for env_name, ref in {
      NOVAFABRIC_POSTGRES_DSN = var.postgres_dsn_secret_ref
      NOVAFABRIC_METADATA_DSN = var.postgres_dsn_secret_ref
    } : env_name => ref if var.backend == "postgres" && ref != null
  }

  # The shebang is prepended here rather than kept in the template, so the
  # repository holds no non-executable file that starts with "#!".
  install_script = join("", ["#!/bin/sh\n", templatefile("${path.module}/templates/install-novafabric.sh.tftpl", {
    venv_dir          = local.venv_dir
    version           = var.novafabric_version
    package_index_url = var.package_index_url == null ? "" : var.package_index_url
  })])

  systemd_unit = templatefile("${path.module}/templates/novafabric.service.tftpl", {
    backend     = var.backend
    config_dir  = local.config_dir
    env_file    = local.env_file
    listen_host = local.listen_host
    port        = var.server_port
    service     = local.service_user
    state_dir   = local.state_dir
    venv_dir    = local.venv_dir
    workers     = var.workers
  })

  env_example = templatefile("${path.module}/templates/server.env.example.tftpl", {
    backend           = var.backend
    env_file          = local.env_file
    secret_references = local.secret_references
    state_dir         = local.state_dir
  })

  caddyfile_example = templatefile("${path.module}/templates/Caddyfile.example.tftpl", {
    hostname    = local.hostname
    listen_host = local.listen_host
    port        = var.server_port
  })

  nginx_example = templatefile("${path.module}/templates/nginx.conf.example.tftpl", {
    hostname    = local.hostname
    listen_host = local.listen_host
    port        = var.server_port
  })

  cloud_config = {
    package_update  = true
    package_upgrade = false
    packages        = ["python3-venv", "ca-certificates"]

    users = [
      "default",
      {
        name        = local.service_user
        system      = true
        homedir     = local.state_dir
        shell       = "/usr/sbin/nologin"
        lock_passwd = true
      },
    ]

    # defer = true writes the files in the final stage, after the users module
    # has created the service account.
    write_files = [
      {
        path        = "${local.config_dir}/server.yaml"
        permissions = "0644"
        owner       = "root:root"
        defer       = true
        content     = yamlencode(local.server_config)
      },
      {
        path        = "${local.config_dir}/server.env.example"
        permissions = "0644"
        owner       = "root:root"
        defer       = true
        content     = local.env_example
      },
      {
        path        = "/usr/local/sbin/install-novafabric"
        permissions = "0755"
        owner       = "root:root"
        defer       = true
        content     = local.install_script
      },
      {
        path        = "/etc/systemd/system/novafabric.service"
        permissions = "0644"
        owner       = "root:root"
        defer       = true
        content     = local.systemd_unit
      },
      {
        path        = "${local.config_dir}/reverse-proxy/Caddyfile.example"
        permissions = "0644"
        owner       = "root:root"
        defer       = true
        content     = local.caddyfile_example
      },
      {
        path        = "${local.config_dir}/reverse-proxy/nginx.conf.example"
        permissions = "0644"
        owner       = "root:root"
        defer       = true
        content     = local.nginx_example
      },
    ]

    # The unit is enabled on every backend. With backend = "postgres" it carries
    # ConditionPathExists on the env file, so it is skipped (not failed, no
    # restart loop) until the operator has written the DSN there.
    runcmd = [
      ["/usr/local/sbin/install-novafabric"],
      ["systemctl", "daemon-reload"],
      ["systemctl", "enable", "novafabric.service"],
      ["systemctl", "start", "--no-block", "novafabric.service"],
    ]
  }

  user_data = "#cloud-config\n${yamlencode(local.cloud_config)}"

  ingress_rules = [
    for rule in [
      {
        name        = "ssh"
        protocol    = "tcp"
        port        = 22
        cidrs       = var.admin_cidrs
        description = "SSH for administrators"
      },
      {
        name        = "https"
        protocol    = "tcp"
        port        = 443
        cidrs       = var.client_cidrs
        description = "TLS reverse proxy in front of the loopback NovaFabric API"
      },
    ] : rule if length(rule.cidrs) > 0
  ]

  labels = merge(
    {
      app                = "novafabric"
      managed-by         = "opentofu"
      novafabric-version = replace(var.novafabric_version, ".", "-")
    },
    var.labels,
  )
}
