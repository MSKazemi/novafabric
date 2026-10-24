//go:build cgo

// Package main is the entry point for the c-shared build of libnovaspool.so.
//
// Build with:
//
//	cd collector && go build -buildmode=c-shared -o ../lib/libnovaspool.so ./pkg/cffi/
//
// The exported C functions allow Python workloads (via cffi) to write events
// directly to the local spool without spawning the Go collector binary.
//
// Thread-safety: SpoolWrite is goroutine-safe via the internal spool mutex, and
// safe across processes sharing a spool directory (link(2) commit).
// SpoolLastError is NOT goroutine-safe (uses a module-level variable for
// simplicity; suitable for demo / single-writer use cases).
package main

/*
#include <stdlib.h>
#include <stdint.h>
*/
import "C"

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"unsafe"

	"github.com/MSKazemi/novafabric/collector/internal/spool"
)

// lastError stores the most recent error string from SpoolWrite / SpoolDrain.
// Accessed via SpoolLastError.  Not goroutine-safe — suitable for single-writer
// use and demonstration purposes.
var lastError string

// spoolSeq is this process's next sequence number: a floor, never trusted alone.
// Each write takes the maximum of it, every committed segment in the directory
// and the forwarder's checkpoint (review go-collector-ts-packages-03).
var spoolSeq uint64

// spoolMu serialises concurrent SpoolWrite calls in this process.
var spoolMu sync.Mutex

// headerSize is the fixed byte length of the segment header (mirrors spool/segment.go).
const headerSize = 16

// headerVersion is the only supported segment format version.
const headerVersion = 1

// maxCommitAttempts bounds the retries when another process takes a sequence first.
const maxCommitAttempts = 64

// SpoolWrite writes a single JSON event to the local spool directory at
// spoolDir.  The event is written to a new <seq>.jsonl file using the same
// 16-byte-header format as the Go spool package.
//
// Returns 0 on success, -1 on error.  Call SpoolLastError to retrieve the
// error string after a -1 return.
//
//export SpoolWrite
func SpoolWrite(spoolDir *C.char, eventJSON *C.char, eventLen C.int) C.int {
	dir := C.GoString(spoolDir)
	data := C.GoBytes(unsafe.Pointer(eventJSON), eventLen)
	if err := writeSegment(dir, data); err != nil {
		lastError = err.Error()
		return -1
	}
	return 0
}

// writeSegment commits one event as a new segment, never replacing another.
//
// The library is loaded into every Python capture process, so a per-process
// counter starting at 0 replaced another run's undrained segment, and — once the
// forwarder had acknowledged up to K — wrote events at seqs 0..K that the reader
// skips and eviction deletes as already acknowledged. The sequence now starts
// above every committed segment and the checkpoint, and the segment is committed
// with link(2), which fails if the name exists, where rename(2) silently replaces
// it; a collision with another process moves to the next sequence. link is a
// POSIX commit primitive, not a lock, and works on NFS and Lustre.
func writeSegment(dir string, data []byte) error {
	if err := os.MkdirAll(dir, 0o750); err != nil {
		return fmt.Errorf("SpoolWrite: mkdir %s: %w", dir, err)
	}
	spoolMu.Lock()
	defer spoolMu.Unlock()

	seq, err := nextSeq(dir)
	if err != nil {
		return err
	}
	// The temp name carries the PID, so two processes never share one; it keeps
	// the .jsonl.tmp suffix the spool's crash recovery looks for.
	tmpPath := filepath.Join(dir, fmt.Sprintf("%020d.%d.jsonl.tmp", seq, os.Getpid()))
	if err := writeTemp(tmpPath, seq, data); err != nil {
		_ = os.Remove(tmpPath)
		return err
	}
	defer func() { _ = os.Remove(tmpPath) }()

	for attempt := 0; attempt < maxCommitAttempts; attempt++ {
		dstPath := filepath.Join(dir, fmt.Sprintf("%020d.jsonl", seq))
		err := os.Link(tmpPath, dstPath)
		if err == nil {
			spoolSeq = seq + 1
			return syncDir(dir)
		}
		if !errors.Is(err, fs.ErrExist) {
			return fmt.Errorf("SpoolWrite: commit %s: %w", dstPath, err)
		}
		// Another process committed this sequence first. The header records the
		// sequence, so rewrite it for the next one before retrying.
		seq++
		if err := setHeaderSeq(tmpPath, seq); err != nil {
			return err
		}
	}
	return fmt.Errorf("SpoolWrite: no free sequence after %d attempts", maxCommitAttempts)
}

// nextSeq is one above the highest sequence in use: this process's floor, every
// committed segment in dir, and the checkpoint's last acknowledged sequence.
func nextSeq(dir string) (uint64, error) {
	next := spoolSeq
	entries, err := os.ReadDir(dir)
	if err != nil {
		return 0, fmt.Errorf("SpoolWrite: read %s: %w", dir, err)
	}
	for _, e := range entries {
		name := e.Name()
		if !strings.HasSuffix(name, ".jsonl") {
			continue
		}
		n, err := strconv.ParseUint(strings.TrimSuffix(name, ".jsonl"), 10, 64)
		if err == nil && n+1 > next {
			next = n + 1
		}
	}
	cp, err := spool.LoadCheckpoint(dir)
	if err != nil {
		return 0, fmt.Errorf("SpoolWrite: %w", err)
	}
	if cp.HasAcked() && cp.LastAckSeq()+1 > next {
		next = cp.LastAckSeq() + 1
	}
	return next, nil
}

// writeTemp writes the header, the event and the WriteComplete flag, and fsyncs.
//
// Wire layout (little-endian, mirrors spool/segment.go):
//
//	Bytes  0– 3   Writer PID   (uint32)
//	Bytes  4–11   Sequence     (uint64)
//	Byte  12      WriteComplete (0 = incomplete, set to 1 before commit)
//	Byte  13      Version      (always 1)
//	Bytes 14–15   Reserved     (0)
func writeTemp(path string, seq uint64, data []byte) error {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return fmt.Errorf("SpoolWrite: open tmp %s: %w", path, err)
	}
	var hdr [headerSize]byte
	binary.LittleEndian.PutUint32(hdr[0:4], uint32(os.Getpid()))
	binary.LittleEndian.PutUint64(hdr[4:12], seq)
	hdr[13] = headerVersion
	body := make([]byte, 0, len(data)+1)
	body = append(append(body, data...), '\n')
	for _, step := range []struct {
		what string
		run  func() error
	}{
		{"write header", func() error { _, err := f.Write(hdr[:]); return err }},
		{"write event", func() error { _, err := f.Write(body); return err }},
		{"write complete flag", func() error { _, err := f.WriteAt([]byte{1}, 12); return err }},
		{"fsync", f.Sync},
	} {
		if err := step.run(); err != nil {
			_ = f.Close()
			return fmt.Errorf("SpoolWrite: %s: %w", step.what, err)
		}
	}
	if err := f.Close(); err != nil {
		return fmt.Errorf("SpoolWrite: close: %w", err)
	}
	return nil
}

// setHeaderSeq rewrites the sequence recorded in the temp file's header.
func setHeaderSeq(path string, seq uint64) error {
	f, err := os.OpenFile(path, os.O_WRONLY, 0)
	if err != nil {
		return fmt.Errorf("SpoolWrite: reopen tmp: %w", err)
	}
	var b [8]byte
	binary.LittleEndian.PutUint64(b[:], seq)
	if _, err := f.WriteAt(b[:], 4); err != nil {
		_ = f.Close()
		return fmt.Errorf("SpoolWrite: rewrite header seq: %w", err)
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		return fmt.Errorf("SpoolWrite: fsync: %w", err)
	}
	return f.Close()
}

// syncDir makes the new directory entry durable.
func syncDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return fmt.Errorf("SpoolWrite: open dir for fsync: %w", err)
	}
	defer d.Close()
	if err := d.Sync(); err != nil {
		return fmt.Errorf("SpoolWrite: fsync dir: %w", err)
	}
	return nil
}

// SpoolDrain signals the spool to flush all pending events.  In the current
// implementation this is a no-op synchronisation point; the spool is always
// durable after each SpoolWrite.  Future versions may flush a write-ahead
// buffer here.
//
// Returns 0 on success, -1 on error.
//
//export SpoolDrain
func SpoolDrain(spoolDir *C.char) C.int {
	// The cffi spool writes each event atomically on Write; there is no pending
	// buffer to flush.  This export is a hook for future asynchronous batching.
	_ = C.GoString(spoolDir) // validate the pointer is accessible
	return 0
}

// SpoolLastError returns the last error string from SpoolWrite or SpoolDrain.
// The returned pointer is valid until the next SpoolWrite or SpoolLastError call.
// This function is NOT goroutine-safe.
//
//export SpoolLastError
func SpoolLastError() *C.char {
	return C.CString(lastError)
}

// main is required for c-shared build mode.
func main() {}
