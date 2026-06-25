// Package spool implements a durable on-disk event queue for the NovaFabric
// collector. The queue survives process crashes via atomic renames; it never
// uses flock, mmap, or fcntl primitives.
package spool

import (
	"encoding/binary"
	"fmt"
	"os"
	"path/filepath"
)

// headerSize is the fixed byte length of every segment file header.
const headerSize = 16

// headerVersion is the only supported segment format version.
const headerVersion = 1

// segmentHeader represents the 16-byte prefix written at the start of every
// segment file.
//
// Wire layout (all little-endian):
//
//	Bytes  0– 3   Writer PID   (uint32)
//	Bytes  4–11   Sequence     (uint64)
//	Byte  12      WriteComplete (0 = incomplete, 1 = complete)
//	Byte  13      Version      (always 1)
//	Bytes 14–15   Reserved     (must be 0)
type segmentHeader struct {
	PID           uint32
	Seq           uint64
	WriteComplete uint8
	Version       uint8
}

// encode serialises the header to exactly headerSize bytes.
func (h segmentHeader) encode() [headerSize]byte {
	var buf [headerSize]byte
	binary.LittleEndian.PutUint32(buf[0:4], h.PID)
	binary.LittleEndian.PutUint64(buf[4:12], h.Seq)
	buf[12] = h.WriteComplete
	buf[13] = h.Version
	// bytes 14-15 remain 0 (reserved)
	return buf
}

// decodeHeader parses exactly headerSize bytes into a segmentHeader.
// Returns an error if the version field is not headerVersion.
func decodeHeader(buf [headerSize]byte) (segmentHeader, error) {
	h := segmentHeader{
		PID:           binary.LittleEndian.Uint32(buf[0:4]),
		Seq:           binary.LittleEndian.Uint64(buf[4:12]),
		WriteComplete: buf[12],
		Version:       buf[13],
	}
	if h.Version != headerVersion {
		return segmentHeader{}, fmt.Errorf("spool: unsupported segment version %d (want %d)", h.Version, headerVersion)
	}
	return h, nil
}

// segmentWriter stages events into a <seq>.jsonl.tmp file and atomically
// promotes it to <seq>.jsonl on commit.
//
// The only atomic commit primitive used is os.Rename.  No flock, mmap, or
// fcntl primitives are used anywhere in this file.
type segmentWriter struct {
	dir     string
	seq     uint64
	tmpPath string
	dstPath string
	file    *os.File
}

// newSegmentWriter opens a new <seq>.jsonl.tmp file, writes the 16-byte header,
// and returns a segmentWriter ready to accept events.
func newSegmentWriter(dir string, seq uint64) (*segmentWriter, error) {
	tmpPath := filepath.Join(dir, fmt.Sprintf("%020d.jsonl.tmp", seq))
	dstPath := filepath.Join(dir, fmt.Sprintf("%020d.jsonl", seq))

	f, err := os.OpenFile(tmpPath, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o600)
	if err != nil {
		return nil, fmt.Errorf("spool: open segment tmp %s: %w", tmpPath, err)
	}

	hdr := segmentHeader{
		PID:           uint32(os.Getpid()),
		Seq:           seq,
		WriteComplete: 0,
		Version:       headerVersion,
	}.encode()

	if _, err := f.Write(hdr[:]); err != nil {
		_ = f.Close()
		_ = os.Remove(tmpPath)
		return nil, fmt.Errorf("spool: write header to %s: %w", tmpPath, err)
	}

	return &segmentWriter{
		dir:     dir,
		seq:     seq,
		tmpPath: tmpPath,
		dstPath: dstPath,
		file:    f,
	}, nil
}

// writeEvent appends a single JSON-encoded event followed by a newline.
func (sw *segmentWriter) writeEvent(data []byte) error {
	buf := make([]byte, len(data)+1)
	copy(buf, data)
	buf[len(data)] = '\n'
	if _, err := sw.file.Write(buf); err != nil {
		return fmt.Errorf("spool: write event to segment %d: %w", sw.seq, err)
	}
	return nil
}

// commit sets the write-complete flag to 1 in the header, fsyncs, then
// atomically renames the .tmp file to .jsonl.
//
// The function seeks back to byte 12 to overwrite the WriteComplete flag
// without rewriting the entire header.
func (sw *segmentWriter) commit() error {
	// Seek to byte 12 (WriteComplete flag) and set it to 1.
	if _, err := sw.file.Seek(12, 0); err != nil {
		return fmt.Errorf("spool: seek in segment %d: %w", sw.seq, err)
	}
	if _, err := sw.file.Write([]byte{1}); err != nil {
		return fmt.Errorf("spool: write complete flag in segment %d: %w", sw.seq, err)
	}
	if err := sw.file.Sync(); err != nil {
		return fmt.Errorf("spool: fsync segment %d: %w", sw.seq, err)
	}
	if err := sw.file.Close(); err != nil {
		return fmt.Errorf("spool: close segment %d tmp: %w", sw.seq, err)
	}
	// Atomic rename: the ONLY commit primitive in the spool.
	if err := os.Rename(sw.tmpPath, sw.dstPath); err != nil {
		return fmt.Errorf("spool: rename segment %d to committed: %w", sw.seq, err)
	}
	return nil
}

// abort closes and removes the .tmp file without committing.
func (sw *segmentWriter) abort() {
	_ = sw.file.Close()
	_ = os.Remove(sw.tmpPath)
}
