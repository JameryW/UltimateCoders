# PRD: dashboard event data 非字符串值丢失（json_to_dashboard_event）

## 背景

/loop 第 40 轮。json_to_* 审计收尾：发现 json_to_dashboard_event 丢非字符串 data 值。

## 清单（已核实 + 修复）

### F88: json_to_dashboard_event 丢非字符串 data 值（LOW-MED，真 bug）

`json_to_dashboard_event`（dashboard_service.rs:547）把 `data` 当 `map<string,string>` 解析，用 `v.as_str().map(...)` **只保留字符串值**，丢弃 number/bool/object。proto `DashboardEventProto.data` 是 `map<string,string>`（必须 string）。

实例：`_record_event("flush_pending", count=count, executed=len(executed))`（app.py:648）--count/executed 是 int -> 被 Rust 丢弃 -> `ev.data={}`。

dashboard 侧 `grpcEventProtoToDashboardEvent`（useDashboardGrpc.ts:123）**期望字符串化值**并还原类型：数字串->Number、`"true"`/`"false"`->bool、`{`/`[`->JSON.parse。`EventLogPanel.eventSummary` 渲染 details（`${first}: ${JSON.stringify(val)}`）。故 int 被丢后，flush_pending 事件无 details 显示。

## 修

`json_to_dashboard_event` 对非字符串值**字符串化**（而非丢弃）：string 原样；number `5`->`"5"`；bool `true`->`"true"`；object/array->JSON 串。dashboard 侧已能还原（Number/bool/JSON.parse）。round-trip 自洽。

- string：原样（不用 `to_string()` 免得带引号）。
- 其它：`serde_json::Value::to_string()`（5->"5"，true->"true"，{...}->JSON）。

影响：flush_pending 的 count/executed 现以 "5"/"3" 传递，dashboard 还原为 Number，eventSummary 显示 `count: 5`。

## 验收

- `cargo check`/`fmt --check`/`clippy`（-p uc-grpc）干净；`cargo test -p uc-grpc` 121+6 全绿。
- `tsc -p tsconfig.app.json --noEmit` 0；`vite build` 通过。
- feature branch + PR + CI green（ci-rust）。

## 不做

json_to_* 审计至此完结：live 路径（workers/metrics/events/scheduler）全核对正确；task/subtask/snapshot 为 messaging-only（default build dead）。scheduler 路径本身 inactive（orch.scheduler None stub）。
