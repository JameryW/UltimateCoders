# PRD: semantic fallback content_snippet 多字节 panic（F90 漏网一处）

## 背景

/loop 第 43 轮。PR #343（F90）修了 4 处 `&t[..200]`/`&code[..200]` 字节切片 panic，但漏了 BLAKE3 fallback 搜索路径的一处（`&f.content[..200]`）。本轮广扫补齐。

## 清单（已核实 + 修复）

### F91: semantic fallback `&f.content[..200]` 多字节 panic（MED，F90 同类）

`indexer/semantic.rs:627`（BLAKE3 fallback 搜索，cosine_similarity 路径）：

```rust
content_snippet: if f.content.len() > 200 {
    format!("{}...", &f.content[..200])
} else {
    f.content.clone()
},
```

与 F90 同：`f.content` 是 String，`&f.content[..200]` 字节切片，多字节 UTF-8（中文注释/字符串）时字节 200 落字符中间 -> panic。F90 的 grep 只匹配 `&t`/`&code` 变量名，漏了 `&f.content`。

广扫确认：crates/ 内剩余 `[..N]` str 字节切片仅此一处（`long_term.rs:555` 的 `hash.as_bytes()[..8]` 是 `&[u8]` 字节切片+固定 32 字节 BLAKE3，非 str，安全）。

## 修

`&f.content[..200]` -> `crate::sandbox::truncate_str(&f.content, 200)`（同 F90 的 char-safe helper）。

## 验收

- `cargo check`/`fmt --check`/`clippy`（-p uc-engine）干净；`cargo test -p uc-engine` 353 全绿。
- feature branch + PR + CI green（ci-rust）。
