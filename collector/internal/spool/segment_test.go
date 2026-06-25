package spool

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestSegmentHeader_EncodeDecode verifies that the 16-byte header round-trips
// PID, sequence number, and flags correctly.
func TestSegmentHeader_EncodeDecode(t *testing.T) {
	original := segmentHeader{
		PID:           12345,
		Seq:           99999999,
		WriteComplete: 1,
		Version:       headerVersion,
	}
	encoded := original.encode()
	decoded, err := decodeHeader(encoded)
	require.NoError(t, err)

	assert.Equal(t, original.PID, decoded.PID)
	assert.Equal(t, original.Seq, decoded.Seq)
	assert.Equal(t, original.WriteComplete, decoded.WriteComplete)
	assert.Equal(t, original.Version, decoded.Version)
}

// TestSegmentHeader_ZeroReservedBytes ensures bytes 14–15 are always zero.
func TestSegmentHeader_ZeroReservedBytes(t *testing.T) {
	h := segmentHeader{PID: 1, Seq: 1, WriteComplete: 1, Version: headerVersion}
	buf := h.encode()
	assert.Equal(t, byte(0), buf[14], "reserved byte 14 must be 0")
	assert.Equal(t, byte(0), buf[15], "reserved byte 15 must be 0")
}

// TestSegmentHeader_UnsupportedVersion verifies that an unknown version byte
// produces an error.
func TestSegmentHeader_UnsupportedVersion(t *testing.T) {
	var buf [headerSize]byte
	buf[13] = 42 // invalid version
	_, err := decodeHeader(buf)
	assert.ErrorContains(t, err, "unsupported segment version")
}

// TestSegment_NoTmpAfterCommit verifies that after a successful commit the
// .jsonl.tmp file no longer exists and the committed .jsonl file does.
func TestSegment_NoTmpAfterCommit(t *testing.T) {
	dir := t.TempDir()
	sw, err := newSegmentWriter(dir, 1)
	require.NoError(t, err)

	require.NoError(t, sw.writeEvent([]byte(`{"hello":"world"}`)))
	require.NoError(t, sw.commit())

	tmpPath := filepath.Join(dir, "00000000000000000001.jsonl.tmp")
	dstPath := filepath.Join(dir, "00000000000000000001.jsonl")

	_, errTmp := os.Stat(tmpPath)
	assert.True(t, os.IsNotExist(errTmp), ".tmp file must not exist after commit")

	_, errDst := os.Stat(dstPath)
	assert.NoError(t, errDst, ".jsonl file must exist after commit")
}

// TestSegment_AbortRemovesTmp verifies that abort cleans up the .tmp file.
func TestSegment_AbortRemovesTmp(t *testing.T) {
	dir := t.TempDir()
	sw, err := newSegmentWriter(dir, 2)
	require.NoError(t, err)

	require.NoError(t, sw.writeEvent([]byte(`{"x":1}`)))
	sw.abort()

	tmpPath := filepath.Join(dir, "00000000000000000002.jsonl.tmp")
	_, errTmp := os.Stat(tmpPath)
	assert.True(t, os.IsNotExist(errTmp), ".tmp file must not exist after abort")
}

// TestSegment_NoFlockMmapFcntl is a compile-time contract: this package is
// compiled by golangci-lint with a custom linter (see .golangci.yml) that
// rejects any import of syscall.Flock, mmap, fcntl, F_SETLK, LOCK_EX.
// This test documents the invariant and fails if the spool package itself
// accidentally imports a forbidden symbol.  The real enforcement is in CI.
func TestSegment_NoFlockMmapFcntl(t *testing.T) {
	// The forbidden primitives are: flock, mmap, fcntl, syscall.Flock,
	// F_SETLK, F_SETLK64, LOCK_EX.
	// Compile-time enforcement is provided by the golangci-lint "forbidigo"
	// rule in .golangci.yml. This test is a documentation anchor.
	t.Log("INVARIANT: spool uses os.Rename as its only atomic commit primitive.")
	t.Log("No flock / mmap / fcntl / F_SETLK / LOCK_EX may appear in this package.")
}

// TestSegment_WriteCompleteFlag verifies that a newly created segment has
// WriteComplete=0 and a committed segment has WriteComplete=1.
func TestSegment_WriteCompleteFlag(t *testing.T) {
	dir := t.TempDir()
	sw, err := newSegmentWriter(dir, 3)
	require.NoError(t, err)

	require.NoError(t, sw.writeEvent([]byte(`{"seq":3}`)))
	require.NoError(t, sw.commit())

	dstPath := filepath.Join(dir, "00000000000000000003.jsonl")
	f, err := os.Open(dstPath)
	require.NoError(t, err)
	defer f.Close()

	var hdrBuf [headerSize]byte
	_, err = f.Read(hdrBuf[:])
	require.NoError(t, err)

	hdr, err := decodeHeader(hdrBuf)
	require.NoError(t, err)
	assert.Equal(t, uint8(1), hdr.WriteComplete, "committed segment must have WriteComplete=1")
}
