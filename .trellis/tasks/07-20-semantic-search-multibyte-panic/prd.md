# PRD: 语义搜索 content_snippet 多字节 panic

## 背景

/loop 第 42 轮。搜索子系统审计续。发现 semantic 搜索 content_snippet 构造用字节切片，多字节内容会 panic。

## 清单（已核实 + 修复）

### F90: semantic content_snippet `&t[..200]` 多字节 panic（MED，真 bug）

semantic 搜索构造 `content_snippet` 时（search/semantic.rs:123,130 + indexer/semantic.rs:568,575）：

```rust
if t.len() > 200 { format!("{}...", &t[..200]) } else { t.clone() }
```

`t.len()` 是字节长度；`&t[..200]` 是字节切片。若内容含多字节 UTF-8（中文注释/字符串、emoji），字节 200 可能落在字符中间 -> `&t[..200]` **panic**（"byte index 200 is not a char boundary"）。代码 chunk 含非 ASCII 很常见（中文注释/字符串）。

`len() > 200` 守卫只保证 200 在界内，**不**保证在 char boundary -> 不防 panic。

## 修

复用现成的 char-safe helper `crate::sandbox::truncate_str(s, max_len)`（sandbox/mod.rs:283，用 `char_indices` 找不切字符的断点，有 multibyte 测试）。4 处 `&t[..200]`/`&code[..200]` 改为 `crate::sandbox::truncate_str(t/code, 200)`，保留 `len() > 200` 守卫（仅截断时加 "..."）。

## 验收

- `cargo check`/`fmt --check`/`clippy`（-p uc-engine）干净；`cargo test -p uc-engine` 353 全绿。
- feature branch + PR + CI green（ci-rust）。

## 不做

无。搜索子系统审计续（AST 语言过滤 F89 已修，本 F90 修，semantic scoring/embedding 可后续）。
