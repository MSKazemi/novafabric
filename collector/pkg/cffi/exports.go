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
// Thread-safety: SpoolWrite is goroutine-safe via the internal spool mutex.
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
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"unsafe"
)

// lastError stores the most recent error string from SpoolWrite / SpoolDrain.
// Accessed via SpoolLastError.  Not goroutine-safe — suitable for single-writer
// use and demonstration purposes.
var lastError string

// spoolSeq is a global monotonic sequence counter per process.  Each SpoolWrite
// call increments it to produce unique segment filenames.  This mirrors the
// per-Spool seqNum field in the Go spool package.
var spoolSeq uint64

// spoolMu serialises concurrent SpoolWrite calls so that sequence numbers are
// assigned without gaps and file renames are atomic.
var spoolMu sync.Mutex

// headerSize is the fixed byte length of the segment header (mirrors spool/segment.go).
const headerSize = 16

// headerVersion is the only supported segment format version.
const headerVersion = 1

// SpoolWrite writes a single JSON event to the local spool directory at
// spoolDir.  The event is written to a new <seq>.jsonl file using the same
// atomic-rename, 16-byte-header format as the Go spool package.
//
// Returns 0 on success, -1 on error.  Call SpoolLastError to retrieve the
// error string after a -1 return.
//
//export SpoolWrite
func SpoolWrite(spoolDir *C.char, eventJSON *C.char, eventLen C.int) C.int {
	dir := C.GoString(spoolDir)
	data := C.GoBytes(unsafe.Pointer(eventJSON), eventLen)

	if err := os.MkdirAll(dir, 0o750); err != nil {
		lastError = fmt.Sprintf("SpoolWrite: mkdir %s: %v", dir, err)
		return -1
	}

	spoolMu.Lock()
	seq := atomic.AddUint64(&spoolSeq, 1) - 1
	spoolMu.Unlock()

	tmpPath := filepath.Join(dir, fmt.Sprintf("%020d.jsonl.tmp", seq))
	dstPath := filepath.Join(dir, fmt.Sprintf("%020d.jsonl", seq))

	f, err := os.OpenFile(tmpPath, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o600)
	if err != nil {
		lastError = fmt.Sprintf("SpoolWrite: open tmp %s: %v", tmpPath, err)
		return -1
	}

	// Write the 16-byte segment header (little-endian).
	// Wire layout:
	//   Bytes  0– 3   Writer PID   (uint32)
	//   Bytes  4–11   Sequence     (uint64)
	//   Byte  12      WriteComplete (0 = incomplete, will be set to 1 before rename)
	//   Byte  13      Version      (always 1)
	//   Bytes 14–15   Reserved     (0)
	var hdr [headerSize]byte
	binary.LittleEndian.PutUint32(hdr[0:4], uint32(os.Getpid()))
	binary.LittleEndian.PutUint64(hdr[4:12], seq)
	hdr[12] = 0 // WriteComplete = incomplete
	hdr[13] = headerVersion

	if _, err := f.Write(hdr[:]); err != nil {
		_ = f.Close()
		_ = os.Remove(tmpPath)
		lastError = fmt.Sprintf("SpoolWrite: write header: %v", err)
		return -1
	}

	// Write the event JSON followed by a newline (same format as spool.writeEvent).
	buf := make([]byte, len(data)+1)
	copy(buf, data)
	buf[len(data)] = '\n'
	if _, err := f.Write(buf); err != nil {
		_ = f.Close()
		_ = os.Remove(tmpPath)
		lastError = fmt.Sprintf("SpoolWrite: write event: %v", err)
		return -1
	}

	// Set WriteComplete = 1 by seeking to byte 12 and overwriting.
	if _, err := f.Seek(12, 0); err != nil {
		_ = f.Close()
		_ = os.Remove(tmpPath)
		lastError = fmt.Sprintf("SpoolWrite: seek for complete flag: %v", err)
		return -1
	}
	if _, err := f.Write([]byte{1}); err != nil {
		_ = f.Close()
		_ = os.Remove(tmpPath)
		lastError = fmt.Sprintf("SpoolWrite: write complete flag: %v", err)
		return -1
	}

	if err := f.Sync(); err != nil {
		_ = f.Close()
		_ = os.Remove(tmpPath)
		lastError = fmt.Sprintf("SpoolWrite: fsync: %v", err)
		return -1
	}
	if err := f.Close(); err != nil {
		_ = os.Remove(tmpPath)
		lastError = fmt.Sprintf("SpoolWrite: close: %v", err)
		return -1
	}

	// Atomic rename — the ONLY commit primitive (matches spool/segment.go).
	if err := os.Rename(tmpPath, dstPath); err != nil {
		_ = os.Remove(tmpPath)
		lastError = fmt.Sprintf("SpoolWrite: rename: %v", err)
		return -1
	}

	return 0
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
