//go:build linux

package spool

import (
	"errors"
	"syscall"
)

// dirSyncUnsupported reports the errors a filesystem returns when it has no directory fsync.
func dirSyncUnsupported(err error) bool {
	return errors.Is(err, syscall.EINVAL) || errors.Is(err, syscall.ENOTSUP)
}
