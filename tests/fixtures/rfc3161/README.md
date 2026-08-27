# RFC 3161 fixtures

`freetsa-response.tsr` is a real DER `TimeStampResp` captured from
`https://freetsa.org/tsr` on 2026-08-27. The nonce sent in the matching request
was `12473090696047252391`.

It exists because hand-built TSRs did not catch a real defect: the nonce
extractor returned the first nonce-sized INTEGER in the response, which in a
genuine TSR is `serialNumber`, three fields ahead of `nonce`. Every synthetic
fixture in the suite passed while timestamping failed against every real TSA.
Keep this file byte-for-byte; regenerating it changes the expected nonce.
