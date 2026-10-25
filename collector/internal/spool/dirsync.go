package spool

import (
	"fmt"
	"os"
)

// syncDirFn is the directory-fsync used after a rename; a variable so tests can observe it.
var syncDirFn = SyncDir

// SyncDir fsyncs a directory so a rename, link or unlink inside it survives power loss. A
// file's own fsync makes its bytes durable, not the directory entry that names it: after a
// crash a segment whose Write had returned could come back as <seq>.jsonl.tmp (review
// go-collector-ts-packages-16). A filesystem that cannot fsync a directory is not an error.
func SyncDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return fmt.Errorf("spool: open dir %s for fsync: %w", dir, err)
	}
	defer d.Close()
	if err := d.Sync(); err != nil && !dirSyncUnsupported(err) {
		return fmt.Errorf("spool: fsync dir %s: %w", dir, err)
	}
	return nil
}
