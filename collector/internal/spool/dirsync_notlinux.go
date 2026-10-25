//go:build !linux

package spool

// dirSyncUnsupported: directory fsync is best-effort off Linux (Windows cannot do it).
func dirSyncUnsupported(error) bool { return true }
