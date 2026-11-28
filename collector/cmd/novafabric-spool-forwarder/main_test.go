package main

import "testing"

// The stream the forwarder creates is the one the Python consumers bind to by
// default (src/novafabric/evidence_fabric/_nats_names.py). A rename here
// strands every consumer that relies on its default.
func TestDefaultEvidenceStreamIsCanonical(t *testing.T) {
	if defaultEvidenceStream != "NOVA_EVIDENCE" {
		t.Fatalf("defaultEvidenceStream = %q, want NOVA_EVIDENCE", defaultEvidenceStream)
	}
	if defaultEvidenceSubjectPrefix != "nova.evidence" {
		t.Fatalf("defaultEvidenceSubjectPrefix = %q, want nova.evidence", defaultEvidenceSubjectPrefix)
	}
}
