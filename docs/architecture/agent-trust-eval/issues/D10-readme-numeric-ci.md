# D10-readme-numeric-ci

Status: **decided** (2026-10-27 — via「继续」adopting Proposed)
Type: grilling
Blocked by: _(none — aligns with D1 / T2 report-only public uplift)_
Blocks: P3 README / docs numeric pin CI tickets

## Question

README / 文档中的数字由哪条脚本复算？CI 失败策略是漂移告警还是 hard fail？与 `eval_role=report` / 禁止 select uplift 如何统一？

## Context

- Map fog：数字复算脚本与 CI 失败策略。
- README 当前几乎没有 uplift `%`；已有可核对的实测数字主要是 SSE 延迟族（EN/ZH README + verification 文档中的毫秒上限）。
- 仓库已有 docs↔machine hard-fail 先例（如 `check-readme-ci-table.py`）与可重测脚本（如 `verify-sse-latency.py`，但依赖 live stack）。
- D1 / T2：对外 uplift 只能来自 report 角色的受保护重测。

## Resolution

**MVP = 文档数字清单 + pin checker hard fail；先钉 SSE 族；live 重测后置。**

1. 维护一份机器可读的数字清单（manifest）：字段含文档路径、断言文本/数字、允许来源脚本或固定 pin。
2. CI：对清单做 **hard fail** 漂移检查（文档改了数字却未更新 pin / 或 pin 与文档不一致即失败）；禁止未登记的 uplift `%` 进入 README 对外数字面。
3. Live 重测（起栈跑 `verify-sse-latency` 等）与 GPU/BenchmarkRunner 复算 **延后** — 不阻塞 pin checker；可选后续票挂在同一地图。
4. 与 D1/T2 一致：任何文档 uplift 百分比必须可追溯到 `eval_role=report` 产物，否则不得入清单。
