# research/notes.md — T21 #672 普查笔记（一手实测，可复算）

## 复算命令

```bash
python scripts/check-spec-refs.py            # 结构性门禁（exit 1 = 有缺陷）
python scripts/check-spec-refs.py --audit    # 追加符号级过期表
python scripts/check-spec-refs.py --json     # 全量行，机器可读
```

消融自检（改一个 spec 文件 → 跑守卫 → 断言 → 按字节恢复 + 校 sha256）：

```bash
python .scratch/t672-ablation.py     # 5 个突变：M0 阴性对照 + M1/M2/M3/M4
```

## 判定规则（三层，优先级从高到低）

1. **结构层**（`MISSING_FILE` / `PATH_FORM` / `OUT_OF_RANGE`）—— 确定性，fail-closed。
   约定：**裸文件名**（`worker.py`）是本仓 spec 的成文写法 ⇒ 唯一命中即 **OK**，多命中为
   advisory `AMBIGUOUS`，零命中为 `MISSING_FILE`。**只有带目录的路径**才按「必须原样命中」判。
2. **符号层**（`STALE`）—— 取同一规范行上被反引号点名的、**在目标文件里真有定义**的符号，
   与其 `def` 行比对；按「离引用行最近的定义」取胜者。advisory，永不判红。
3. **都判不了** —— 记为 `OK`（不猜）。

## 全量结果（148 处 / 12 个 spec / 33 个目标文件）

| 判定 | 数量 |
|---|---|
| `OK` | 121 |
| `STALE`（advisory） | 19 |
| `AMBIGUOUS`（advisory） | 8 |
| 结构性缺陷 | **0**（修掉 §B 两处之后） |

### STALE 的 19 处，按偏移量分档

**大幅（|off| ≥ 100，11 处）** —— 系统性漂移，与 issue 描述同源：

| 偏移 | 文件:行 | 符号 | 真实 `def` 行 |
|---|---|---|---|
| +391 | `worker.py:1426` | `_render_step_prompt` | 1817 |
| +391 | `worker.py:1394` | `_output_to_json` | 1785 |
| +374 | `worker.py:1246` | `_run_single_step` | 1620 |
| +321 | `worker.py:1299` | `_run_single_step` | 1620 |
| +317 | `sandbox.py:1007` | `_parse_agent_file_changes` | 1324 |
| +312 | `worker.py:1053` | `_execute_steps` | 1365 |
| +312 | `worker.py:1035` | `_emit_step_event` | 1347 |
| +304 | `worker.py:1043` | `_emit_step_event` | 1347 |
| +228 | `worker.py:1137` | `_execute_steps` | 1365 |
| +208 | `worker.py:994` | `_execute_in_sandbox` | 1202 |
| +119 | `worker.py:1246` | `_execute_steps` | 1365 — **工具误判，见「局限 1」** |

**小幅（|off| < 100，8 处）** —— **不足以判定过期**（可能有意指向定义上方的注释/属性块）：

`−27` `events.rs:62`、`−21` `crates/uc-engine/src/events.rs:56-72`、
`−13` `memory.py`、`−6` `memory.py`、`+5` `crates/uc-types/src/engine.rs:25`、
`+8` `crates/uc-engine/src/memory/short_term.rs:28`、`+11` `orchestrator.ts`、`+11` `memory.py`

### AMBIGUOUS 的 8 处

全部是 `agent-capability-spec.md` 里的裸 `types.py` —— 仓内存在多个 `types.py`，引用欠定。

## 地面真值校验（工具 vs T20 手工取证）

| issue 给出的引用 | 手工测得的 `def` 行 | 工具输出 | 一致 |
|---|---|---|---|
| `worker.py:1035` `_emit_step_event` | 1347 | 1347 | ✅ |
| `worker.py:1053` `_execute_steps` | 1365 | 1365 | ✅ |
| `worker.py:1246` `_run_single_step` | 1620 | 1620 | ✅ |
| `worker.py:1394` `_output_to_json` | 1785 | 1785 | ✅ |
| `worker.py:1426` `_render_step_prompt` | 1817 | 1817 | ✅ |
| `worker.py:994` `_execute_in_sandbox` | 1202 | 1202 | ✅ |

**6/6 一致** —— 工具复现了手工结论，这是它可被信任的前提。

## 消融自检结果（5/5 PASS）

```
baseline: 149 refs, 2 structural failures, exit 1
  [PASS] M0 阴性对照（合法引用 `worker.py:451`）: probe verdict=OK, structural 2->2
  [PASS] M1 死文件:      MISSING_FILE,  2->3
  [PASS] M2 目录前缀错:  PATH_FORM,     2->3
  [PASS] M3 行号越界:    OUT_OF_RANGE,  2->3
  [PASS] M4 优先级:      PATH_FORM,     2->3   ← 该行同时含可解析符号与坏路径
restored byte-identically (sha 4408d2850bea...)
```

**M4 是把「优先级 bug」钉住的那一条**：同一行既有符号又有坏路径时，必须报**结构**缺陷，
而不是被 advisory 的 `STALE` 覆盖。

## 🔴 工具局限（记账，避免把工具当一手事实）

1. **多引用同行时「最近定义」会选错。** `agent-capability-spec.md:603` 同时引
   `worker.py:1053` 与 `worker.py:1246`；对后者，`_execute_steps`（1365，距 1246 为 119）
   比正确的 `_run_single_step`（1620，距 374）更近 ⇒ 被选中。**该行是 1 处而非 2 处**。
2. **104/148 处引用在同一行上没有可判定符号** ⇒ 工具**判不了**，退化为 `OK`。
   ⇒ 本工具的过期计数是**下界**，不是全量。**别把「工具绿」读成「没有过期引用」。**
3. **小幅偏移不可判**：`±(5~27)` 行既可能是漂移、也可能是有意指向注释块，工具不做断言。
4. **引代码而不点名符号的引用完全在盲区**：`logging-guidelines.md` 的
   「`worker.py:51`: `logger = logging.getLogger(__name__)`」偏 +4，工具抓不到
   —— 需要**内容匹配**（检查第 N 行是否含 `logging.getLogger`）才会红。
   这是口径里「强版本校验器」的价值来源。
5. **索引排除** `target/ vendor/ .venv/ dashboard/ node_modules` 等目录 ⇒ 若将来有引用指向
   这些目录下的文件，会**误报** `MISSING_FILE`（当前 0 例）。
6. 目标文件类型集合含 `.md` ⇒ 规范之间互相引用也会被纳入。
