# Engram System V2: Hyper-Scaled External Knowledge

The Engram system in MiniMind is a localized implementation of the concepts introduced by DeepSeek, designed to provide the model with a massive, deterministic "external memory" of n-gram features.

## 🚀 Key Features

### 1. Two-Stage Execution Flow
To maximize performance and minimize PCIe bottlenecks, the system is split into two phases:
- **Stage 1 (Pre-loop Gather)**: Occurs before the transformer layers. It calculates n-gram hashes and fetches required embeddings from CPU RAM to GPU VRAM in a single batch.
- **Stage 2 (In-loop Fusion)**: Occurs inside specific layers (e.g., Layer 2, 4, 6). It fuses the pre-fetched features into the hidden states using dot-product gating.

### 2. Hyper-Scaled RAM Offloading
Unlike standard embeddings, the Engram table can be stored on **System RAM** (`engram_offload_cpu: True`).
- **Benefit**: You can have an Engram table with millions of entries (huge knowledge capacity) while consuming near-zero GPU VRAM.
- **Efficiency**: Only the tokens present in the current batch are transferred to the GPU during the forward pass.

### 3. Advanced Hashing & Gating
- **XOR Mixing**: Uses polynomial rolling hashes combined with XOR operations to minimize collisions across bigrams and trigrams.
- **Multi-Head Hashing**: Each n-gram is hashed using multiple distinct prime moduli, effectively scaling the "address space" of the table.
- **Dot-Product Gating**: Uses a Cross-Attention style gate where the current `hidden_state` (Query) interacts with the `engram_feature` (Key) to determine the injection strength.
- **Short Convolution**: Applies a depthwise convolution to smoothed engram features, providing local spatial awareness.

## 🛠 Configuration

Configure the Engram system via `MiniMindConfig`:

```python
config = MiniMindConfig(
    use_engram=True,
    engram_offload_cpu=True,    # Enable for hyper-scaling
    max_ngram_size=3,           # Supports bigrams and trigrams
    engram_vocab_size=1048576,  # Size of the unified table
    n_head_per_ngram=8,         # Number of hash heads per n-gram
    engram_layers=[2, 4, 6],    # Target layers for injection
    engram_kernel_size=4        # ShortConv kernel size
)
```

## 🔍 Inference Consistency

A major fix in V2 ensures that during auto-regressive generation (incremental inference with KV-cache), the system preserves the n-gram context. It internally tracks the necessary token history to ensure that a token generated today has the same "memory" hash as it did during training.

## 🧪 Testing

Run the dedicated test suite to verify the system:
```bash
python test/test_engram_v2.py
```
