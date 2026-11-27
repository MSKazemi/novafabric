output "instance_id" {
  description = "Compute Engine instance ID."
  value       = google_compute_instance.server.instance_id
}

output "internal_ip" {
  description = "Internal IPv4 address of the VM."
  value       = google_compute_instance.server.network_interface[0].network_ip
}

output "secret_references" {
  description = "Secret NAMES the operator must resolve into the host's env file (never values)."
  value       = module.server.secret_references
}

output "health_checks" {
  description = "On-host loopback health endpoints."
  value       = module.server.health_checks
}

output "service" {
  description = "Systemd unit, service user, listener and paths on the host."
  value       = module.server.service
}
