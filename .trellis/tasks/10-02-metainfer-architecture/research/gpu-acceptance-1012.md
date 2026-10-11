# R8 local deployment findings (2026-10-12)

Continuation of the existing MetaInfer architecture task; no new implementation
ticket is introduced. PR #707 is merged at 48410a6e after 11 CI checks passed.

The recovered GPU image is
f1b3887a8d71475455501c00d27b26bdf125179a7bdfcd658848623f392efa0f.
Actual OCI execution observed Torch 2.6.0+cu124, CUDA 12.4, Triton 3.2.0 and the
RTX 4060 Laptop GPU. Three fused-add/ReLU cases (4096, 65536, 1048576 elements)
matched Torch with maximum error 0. The container stopped with a durable cgroup
receipt. This is runtime preflight, not accepted MetaInfer optimization.

Codex 0.158 against Ollama 0.40.3 Responses did not create the requested file.
Turning thinking off enabled real tool events, but the full CLI failed through
tool misuse or an empty final answer. No pass is claimed for that path. A
disposable native Ollama tool client inside the same OCI boundary executed two
actual shell calls, created smoke.py and ran its requested check. A second OCI
job independently verified positive, negative and fractional inputs. Its model
digest was c604d8c2496349d93c02ae1a4ab8de30f26c099df61405a958cf366e618e7b72.

Next seam to verify against pinned upstream: evolve-kernel writes candidates
into its generated workspace and kernel_library.json, whereas UC's Oracle
benchmarks the assigned repository. Source inspection found no copy of the
winner back into the original kernel_file_path. Do not claim an accepted
repository optimization until a real run demonstrates the publication path.
Any adapter must keep publication inside the owned OCI scope and leave the UC
protected benchmark and Oracle authoritative. No upstream fork is needed.

Hardware HTTP remains 503. Full optimization, protected Oracle accept/reject,
rollback, delivery and active-run recovery remain OPEN.

Native Gemma model digest
0979dc60650788ee9dc535e6d808fa5ac9b7790f33f38d0a207bab93caa7291f
also passed a coding smoke, including two real shell tools and independent
positive/negative/fractional assertions. A streaming client repeated this pass
without converting prose into tool calls. The client is a disposable native
Ollama deployment probe with a pinned upstream-compatible event format; it is
not the official Codex CLI and is not a production fallback.

The actual Qwen optimization stopped in correctness-harness generation with
logic failures. Gemma's nonstreaming attempt timed out. Both retained immutable
container and cgroup stop receipts and neither changed the repository. A new
streaming run created a correctness harness and reached its reviewer, which
rejected missing integer/extreme-value coverage. This is progress through real
upstream phases, not accepted optimization.

The publication fix uses uc.metainfer.publisher=1 and kernel_publication
negotiation. The UC adapter refetches after quiescence and checks repository
bytes. An actual OCI controlled-publication fixture passed on immutable image
679431c52a129e3a8b36b8cff21142f82be1b3fff6eda49a14b835e3f7a09373.
Its receipt leaves Oracle acceptance pending. See gpu-verification.json for
runtime, model, failure and controlled-fixture evidence; gpu_release_acceptance
is false throughout.

The hardware continuation supersedes the earlier unimplemented endpoint status.
The explicit image-owned profile now passes real CUDA/Triton and model nonce
attestation, production native CLI tools, independent code assertions, cached
launcher recovery during live writes and the public HTTP GPU deployment gate.
Default CPU hardware remains unavailable. Neither profile preflight nor a
successful coding smoke establishes MetaInfer optimization acceptance.

PR #708 merged at 6db2189e with all 11 CI checks. The prior streaming trial was
stopped after repeated harness review failure; direct Git baseline comparison
confirmed unchanged repository bytes. A new trial uses an explicit input
contract in its reference kernel, without modifying pinned upstream prompts
or weakening UC's protected Oracle. R8 and actual two-host delivery remain OPEN.
