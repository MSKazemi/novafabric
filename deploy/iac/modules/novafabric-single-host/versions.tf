# Provider-neutral NovaFabric single-host module (experimental).
#
# This module declares NO providers and creates NO resources. It only renders
# inputs - cloud-init user-data, ingress intent, sizing and health-check
# endpoints - that a provider-specific root (deploy/iac/examples/*) wires into
# its own instance, firewall and disk resources. `tofu init`, `tofu validate`
# and `tofu test` (plan-only) therefore run offline, with no account and no
# credentials.
terraform {
  # `tofu test` (the contract tests in tests/) needs 1.6+. Cross-input rules
  # are output preconditions rather than cross-variable validation, so the
  # module does not need 1.9+. CI pins one exact OpenTofu release.
  required_version = ">= 1.6.0"
}
