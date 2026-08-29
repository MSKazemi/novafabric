# For platform and engineering teams

**The page for the person who has to say yes.** You have been asked to evaluate
NovaFabric for a team, and someone will ask you whether it is safe to depend on.
This page answers that in the order those questions actually get asked, links to the
evidence rather than asserting, and tells you plainly where the answer is "not yet".

If you want the product story instead, start at the [README](../README.md). If you
are evaluating for a paper, [For researchers](for-researchers.md) is the better page.
If you are evaluating for an audit or compliance function, read
[Assurance cases](assurance-cases.md) — it explains why NovaFabric emits receipts and
never verdicts.

---

## The 60-second answer

| Question | Answer |
|---|---|
| **License** | Apache-2.0. No CLA required to *use*; a CLA applies to contributions ([CLA.md](../CLA.md)). |
| **Maturity** | **Beta, v0.101.0.** Local capture/replay/diff/lineage are stable and used daily. Server mode, the collector, the dashboard, and the at-scale lineage backends are `experimental`. |
| **Format stability** | On-disk capsule and evidence-bundle formats are **not frozen** until the v1.0 schema freeze. Do not build a long-lived external contract on capsule internals yet. |
| **Runtime dependency on us** | **None.** No accounts, no telemetry, no license server, no phone-home, no update check. If this project disappeared tomorrow, your capsules keep working — they are folders. |
| **Where data lives** | Your infrastructure only. Local mode writes to `~/.novafabric/`; server mode writes to your own Postgres. |
| **Network requirement** | Core features work fully offline and air-gapped. |
| **Bus factor** | **One maintainer today.** This is the honest headline risk — see [below](#the-risks-we-would-raise-if-we-were-you). |

---

## Does it fit our stack?

**Capture requires no application code changes.** `nova capture <command>` wraps any
command. That is the whole integration for most teams.

| You run | How NovaFabric attaches |
|---|---|
| Python agents / scripts | Auto-hooked SDKs: OpenAI, Anthropic, MCP, httpx, requests, aiohttp, urllib3, Bedrock |
| Non-Python clients | `nova api-proxy` / `nova mcp-proxy` — wire-level, language-agnostic |
| Frameworks | Adapters for LangGraph, CrewAI, AutoGen, DSPy, LlamaIndex, Pydantic AI, Haystack, Google ADK, OpenAI Agents, Bedrock AgentCore, MLflow, Langfuse, A2A |
| CI | The [GitHub Action](../.github/actions/capture/README.md) — three lines of YAML |
| Kubernetes | Helm chart and manifests in [`deploy/`](../deploy/) |
| HPC / Slurm | [`deploy/hpc/`](../deploy/hpc/) |
| Air-gapped sites | `make airgap-bundle` — one signed tar, verifiable with zero network |

**Python 3.12+.** That is a real constraint; check it before anything else.

---

## Security and supply chain

Start with [SECURITY.md](../SECURITY.md) (disclosure process) and
[Trust surfaces](trust-surfaces.md) (the signing, attestation, and timestamping
layers, and what each one actually proves).

What is in place today:

- **Secret redaction on the capture path**, with a redaction proof written into every
  capsule — the capsule records *that* redaction happened, so a reviewer can check it.
- **Signed container images** — keyless [cosign](https://docs.sigstore.dev/) signing
  plus SLSA build provenance attestation and an SBOM attached as OCI artifacts
  ([`publish-image.yml`](../.github/workflows/publish-image.yml)).
- **Pinned deploy images** — [`deploy/IMAGE_PINS.md`](../deploy/IMAGE_PINS.md).
- **OpenSSF Scorecard** runs in CI; the badge on the README is live, not decorative.
- **Dependency licence policy** — default-deny on AGPL, SSPL, BSL, source-available,
  GPL, and Elastic, enforced by a CI job
  ([`license-policy.yml`](../.github/workflows/license-policy.yml); the policy itself
  is recorded as a decision, see [decisions](decisions.md)). If your legal team asks whether
  a copyleft dependency can appear in a future version: the gate blocks it in CI.
- **`pip-audit`** on a schedule, with an explicit waiver file under review.

What is **not** claimed:

- No SOC 2, ISO 27001, HIPAA, or FedRAMP certification, and none is planned during
  v0.x. This is a deliberate decision, not an oversight — see
  [Assurance cases](assurance-cases.md).
- Capture is defence-in-depth, not a sandbox. NovaFabric does not claim to contain a
  hostile workload.

---

## Operating it

- [Operator guide](operator-guide.md) — deployment, configuration, backups.
- [SLO catalog](slo.md) — the service levels the server mode is written against.
- [Support policy](support-policy.md) — version support windows and what "supported"
  means for a v0.x project.
- [Benchmarks](benchmarks.md) — capture overhead, measured, with the command and
  hardware stated so you can reproduce it rather than trust it.

**Failure posture:** by design, a NovaFabric component failing must not block your
workload. If capture breaks, your job still runs. Verify this yourself — it is the
single most important property for anyone putting this in a production path.

---

## The risks we would raise if we were you

We would rather you hear these from us than find them in week three.

1. **Bus factor of one.** There is one maintainer. The mitigations are real but
   partial: Apache-2.0, no hosted dependency, capsules are plain folders readable
   without this tool, and the path to becoming a maintainer is written down and open
   ([maintainer criteria](governance/maintainer-criteria.md)). If your organization
   needs a second maintainer to exist before adopting, **that is a reasonable
   position** — and funding or staffing one is the most useful thing a company can do
   for this project.
2. **v0.x formats are not frozen.** If you need a stable on-disk contract today,
   wait for v1.0 or pin a version and plan a migration.
3. **Most subsystems are `experimental`.** The README labels each one. Take those
   labels literally; they are maintained deliberately and the project has a standing
   rule against blurring them.
4. **Small community.** Few external users means fewer people have hit the bug you
   are about to hit. The flip side is that your issue gets a real answer quickly.

---

## What adoption gets you *from* the project

- **A stated response target** — see [CONTRIBUTING.md](../CONTRIBUTING.md) and
  [SUPPORT.md](../SUPPORT.md). It is an honest target, not an SLA.
- **Influence on the roadmap that is disproportionate to your size right now.** A
  team that lists itself in [ADOPTERS.md](../ADOPTERS.md) with a concrete use case
  is, today, the strongest input to [ROADMAP.md](../ROADMAP.md) that exists.
- **A deprecation window that considers you.** The support policy is written against
  what adopters actually run — which only works if you tell us.

---

## How to run a real evaluation in an afternoon

Do not evaluate this from the README. Run it.

```bash
pip install novafabric

# 1. Capture something of your own. No code changes, no keys needed.
nova capture <your command>

# 2. Read the capsule. It is a folder — open it, inspect it, be suspicious of it.
nova validate ~/.novafabric/capsules/<ulid>
ls -R ~/.novafabric/capsules/<ulid>

# 3. Prove the redaction did what it says.
#    Put a fake secret in the environment first, then grep the capsule for it.

# 4. Replay it offline, with the network off.
nova replay <ulid> --mode forensic

# 5. Check the failure posture: break NovaFabric deliberately and confirm
#    your workload still completes.
```

Step 3 and step 5 are the ones that matter. Everything else is documentation you
could have read.

**Found a gap?** [Open an issue](https://github.com/MSKazemi/novafabric/issues/new/choose)
— an evaluation that failed is more useful to this project than one that passed, and
it will get a direct answer. If it is security-sensitive, follow
[SECURITY.md](../SECURITY.md) instead of opening a public issue.
