# Changelog

## [2026-04-17] - Hyper-Scaled Engram V2 & Recurrence (Transformer-XL) Integration

### Added
- **Segment-Level Recurrence (Transformer-XL)**: Implemented stateful training via `mems` propagation between segments.
  - Added `detach()` mechanism to stop-gradient between segments for VRAM efficiency.
  - Supports dynamic sequence length and memory length configuration (`mem_len`).
- **Engram V2 Architecture**: Completely redesigned the Engram system into a Two-Stage Manager (Gather & Fusion).
  - **CPU Offloading**: Support for RAM-resident embedding tables, enabling hyper-scaled knowledge capacity.
- **Unified Output Class**: Introduced `MiniMindLMOutputWithPast` to support `next_mems` and custom metadata in model outputs.
- **Cross-Feature Synergy**: Verified and stabilized the concurrent usage of Recurrence, Dense Attention, and Engram V2.

### Fixed
- **Dense Attention Causal Mask**: Fixed information leakage by correctly applying `diagonal` offset in multi-layer attention.
- **Dynamic RoPE Alignment**: Fixed dimension mismatch in `apply_rotary_pos_emb` by calculating separate RoPE offsets for Q and K during Recurrence.
- **Batch Size Robustness**: Fixed training crashes on incomplete final batches by implementing automatic `mems` reset on batch size mismatch.
- **Flash Attention Incompatibility**: Implemented automatic fallback from Flash Attention when using Recurrence to ensure correct masking.
- **Context Loss in Inference**: Resolved the bug where `model.generate()` would lose n-gram context during KV-caching.

### Changed
- Refactored `MiniMindModel.forward` and `MiniMindForCausalLM` to support stateful `mems` propagation.
- Updated `train_pretrain.py` and `train_reasoning.py` to support recurrence training CLI arguments.
