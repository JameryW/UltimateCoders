# 13 — 失败签名归一化 (T13)

**What to build:** 把 `FAILURE_SIGNATURE` 从「错误文本截断」变成可比对的签名：
同一类失败（不同路径/行号/UUID/十六进制/数字、带 ANSI 的输出）落到同一个字符串，
Traceback 取最后一行异常，无法归一化时诚实地返回空串而不是编造通用签名。

**Blocked by:** T07（证据账本）、T09（采集接线）
**Spec:** [spec-p3.md](../spec-p3.md)
**Status:** implemented（`python/ultimate_coders/knowledge/signatures.py::
normalize_failure_signature`；worker 在失败路径写
`payload={"signature": <归一化>, "excerpt": <原文 500>}`；
`tests/python/test_failure_signatures.py` 9 tests）

**Implementation**

1. 输入先 `safe_text(limit=4000)`（NUL 剥离、超长截断），空输入返回 `""`。
2. 去 ANSI（`_ANSI_ESCAPE_RE`），再取「消息核」：含
   `Traceback (most recent call last)` 时取最后一行形如
   `^[A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit)\b` 的异常行；否则取第一行非空。
3. 归一化可变部分：路径 → `<path>`、UUID → `<id>`、十六进制（含 `0x` 与裸长串）→
   `<hex>`、数字 → `<n>`；压缩空白；最后 `safe_text(limit=200)`。
4. 全是易变内容（例如纯数字/纯哈希）时结果为空串：调用方按「无签名」处理，
   不做「重复失败率下降」的承诺。

**Acceptance gates**

- [x] 两个仅路径/行号不同的错误归一化后相等。
- [x] UUID / hex / 数字被替换；Traceback 只保留异常行。
- [x] ANSI 被剥离；长度上限 200（超出以 `...` 结尾）。
- [x] 不可归一化输入返回 `""`，不编造签名。
- [x] payload 同时保留 `excerpt` 原文（≤500）用于人工诊断；证据不含 diff/prompt。
- [ ] 在 A-B 数据里校准粒度（签名过粗会合并不同失败，见 [11](11-recovery-injection.md)
      的端到端指标；校准前不对外承诺比率改善）。
      观测工具已就绪：`knowledge/metrics.py --signatures` 输出失败签名直方图，
      判读步骤见 [runbook-ab-evaluation.md](../runbook-ab-evaluation.md)。
      2026-11-15 真实样本已出现三类签名：模型连接错误（失败注入）、
      `Stopped after max turns (<n>) without done()`（`max_turns=1` 注入）、
      空直方图（成功样本）；正式校准仍需正常任务语义下的混合失败数据。

**Rollout:** 随 `UC_KNOWLEDGE_STAGE`≥`shadow` 采集；默认关。
**Risks:** 归一化是启发式；异常类型行缺失时退化为「首行」，可能仍含项目个性化文本。
