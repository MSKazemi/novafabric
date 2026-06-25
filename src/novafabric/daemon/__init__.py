"""Warm capture daemon (resident-emitter slice A).

Prefork ``AF_UNIX`` server plus a stdlib-only thin client. Pays the
``novafabric`` import cold-start once at daemon startup, then ``os.fork()``s one
isolated worker per run (copy-on-write warm; "one run = one process, one capsule
= one writer"). See ADR-0092 (extends ADR-0020).
"""

PROTO_VERSION = 1
