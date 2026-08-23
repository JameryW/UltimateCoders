# Domain Documentation

UltimateCoders uses a single domain context. Before designing or implementing a feature, read the repository's `AGENTS.md` and the relevant `.trellis/spec/` guidance. Those describe the current architecture and engineering contracts.

When present, also read:

* `CONTEXT.md` for canonical domain vocabulary.
* `docs/adr/` for durable, decision-specific context.

`CONTEXT.md` and `docs/adr/` are created lazily by `/domain-modeling` or `/grill-with-docs`: only write them when a domain term is resolved or a hard-to-reverse, surprising trade-off is decided. Keep implementation details in specs and tasks, not in the glossary.
