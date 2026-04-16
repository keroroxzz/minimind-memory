# Changelog

## [2026-04-17] - Hyper-Scaled Engram V2 Upgrade

### Added
- **Engram V2 Architecture**: Completely redesigned the Engram system from a single-layer module to a production-grade Two-Stage Manager.
- **Stage 1: Deterministic Gather**: Implemented pre-prefetching logic that queries the Engram table before the Transformer loop.
- **Stage 2: Layer Fusion**: Implemented in-loop fusion with Cross-Attention Gating and Short Convolution.
- **CPU Offloading**: Added support for keeping the massive Engram Embedding table on CPU RAM (`engram_offload_cpu`), enabling hyper-scaled memory (millions of entries) without GPU VRAM exhaustion.
- **Unified Hashing**: Integrated XOR-based n-gram mixing (Bigrams and Trigrams) with multi-head prime-modulus hashing.
- **Incremental Inference Support**: Fixed the "lost context" bug during generation; the model now maintains n-gram context even with KV-caching.
- **Unit Testing**: Added `test/test_engram_v2.py` for verifying hashing, offloading, and equivalence.

### Fixed
- Fixed an issue where `model.generate()` would lose n-gram context after the first token.
- Resolved `IndentationError` and `TypeError` in `model_minimind.py` related to the new module integration.
- Fixed device mismatch errors when `offload_cpu` was enabled.

### Changed
- Refactored `MiniMindModel.forward` and `MiniMindForCausalLM` to pass full sequence history down to the Engram system.
- Updated `MiniMindConfig` with new parameters: `max_ngram_size`, `engram_vocab_size` (unified), `engram_offload_cpu`, and `n_head_per_ngram`.
