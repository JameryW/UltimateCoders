//! # UltimateCoders - Core Engine
//!
//! Implements the `EngineApi` trait with local (in-process) components.
//! Depends on uc-types for the trait and data definitions.

pub mod local;
pub mod indexer;
pub mod memory;
pub mod scheduler;
pub mod search;
pub mod git;
pub mod config;
pub mod metadata;

pub use local::LocalEngine;
pub use config::{EngineConfig, StorageConfig, MemoryConfig, EmbeddingConfig};
pub use indexer::IndexPipeline;
pub use indexer::semantic::{EmbeddingService, SemanticIndexer};
pub use search::HybridSearchEngine;
pub use search::SemanticSearchEngine;
