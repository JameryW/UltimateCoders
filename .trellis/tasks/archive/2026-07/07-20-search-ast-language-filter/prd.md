# PRD: 搜索 AST 语言过滤用错字段（symbol_kind != language）

## 背景

/loop 第 41 轮。转搜索子系统审计（hybrid Text+Semantic+AST）。

## 清单（已核实 + 修复）

### F89: search_ast 的 languages 过滤比 symbol_kind（MED，真 bug）

`HybridSearchEngine::search_ast`（hybrid.rs:120-127）对 AST 结果应用 `languages` 过滤：

```rust
results.retain(|item| item.symbol_kind.as_ref()
    .map(|k| query.languages.contains(k)).unwrap_or(false));
```

但 `SearchQuery.languages` 是**编程语言**（rust/python/...，search.rs:15），而 `SearchResultItem.symbol_kind` 是**符号类型**（"function"/"struct"/"call"，由 ast.rs:207/232/255/277 用 `format!("{:?}", s.kind).to_lowercase()` 等填）。

-> 任何 symbol_kind 都不等于 "rust"/"python" -> 设了 languages 过滤时，AST 结果**全被丢**。

对比：text 搜索按 `meta.language` 过滤（text.rs:356，正确），semantic 按 language tag 过滤（semantic.rs:104，正确）。仅 AST 用错字段。dashboard `SearchPanel` 发 `languages: [language]`（用户填了语言时，见 round 33），故 bug 可达：按语言过滤搜索会静默丢掉所有 AST 匹配。

## 修

`search_ast` 的 languages 过滤改用文件的编程语言（由扩展名推导），与 text 搜索一致：

```rust
results.retain(|item| crate::git::detect_language(&item.file_path)
    .map(|lang| query.languages.iter().any(|l| l.as_str() == lang))
    .unwrap_or(false));
```

`detect_language`（git.rs:112）返 "rust"/"python"/...，与 dashboard 发的 language 名一致。且 indexer 存 `meta.language` 时也用 `detect_language`（indexer/mod.rs:347/485/599/631），故 query-time 推导与 index-time 存储完全一致，与 text 过滤行为对齐。未知扩展名（detect_language 返 None）按过滤丢弃，与 text（meta.language 不在过滤集即丢）一致。

## 验收

- `cargo check`/`fmt --check`/`clippy`（-p uc-engine）干净；`cargo test -p uc-engine` 353 全绿。
- feature branch + PR + CI green（ci-rust）。

## 不做

`Hybrid` mode 与其它 mode 混用（如 [Hybrid, Text]）会让 text 跑两遍、合并时分数被 boost--边缘 case，暂不动。
