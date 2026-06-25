//go:build linux

package spool

import (
	"os"
	"syscall"
)

// fdatasyncFile calls syscall.Fdatasync on the file's underlying file
// descriptor.  Fdatasync flushes data (but not necessarily metadata) to
// stable storage, which is sufficient to guarantee data durability before
// the checkpoint rename.
func fdatasyncFile(f *os.File) error {
	return syscall.Fdatasync(int(f.Fd()))
}
