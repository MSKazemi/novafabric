//go:build !linux

package spool

import "os"

// fdatasyncFile calls f.Sync() on non-Linux platforms, which provides an
// equivalent data-durability guarantee before the checkpoint rename.
func fdatasyncFile(f *os.File) error {
	return f.Sync()
}
