# FIX_TODO — LoopedTransformer / CompleteAttention / Engram bug audit

Audit date: 2026-07-31. Branch: `CompleteAttention`.

All findings below were confirmed by execution (probe scripts) unless marked *(inspection)*.
Line numbers refer to `model/model_minimind.py` at commit `0ff7f8b` unless stated otherwise.

Status legend: `[ ]` open · `[x]` fixed · `[~]` partially fixed / mitigated

---

## Critical

- [x] **C1 — `use_latent_attention=1` crashes on every forward** — `:346-347`
  `expand(..., n_local_heads, kv_lora_rank)` then `.reshape(..., n_local_heads, latent_head_dim)`
  is a numel mismatch (`B*L*H*rank` vs `B*L*rank`).
  → `RuntimeError: shape '[1,8,8,16]' is invalid for input of size 8192`.
  The whole latent path (`readme_dense_attention.md` §1.2) is dead code.
  Sub-issues: `q_norm`/`k_norm` built with `latent_head_dim` but never applied (`:320-321`);
  `v_content = k_content.clone()` makes V bit-identical to K's content half (`:348`);
  `kv_lora_rank % n_local_heads != 0` silently truncates.
  **Fixed**: the `expand`+`reshape` pair is replaced by a straight per-head `view` of
  `kv_lora_rank` (matching how Q already splits its content), which is the layout `o_proj`'s
  `kv_lora_rank` input dim always implied. `q_norm`/`k_norm` are now applied; V keeps the
  un-normed latent; the shared RoPE broadcast across heads is retained (MLA design);
  an indivisible `kv_lora_rank` now raises. Covered by
  `test/test_bugfix_regressions.py::TestLatentAttention`, including latent+dense composition
  and prefill/decode equivalence.

- [x] **C2 — Dense attention ignores the padding mask during decode** — `:404-424`
  At `seq_len==1` the `& causal_mask` bool conversion at `:420` never runs, so `extended_mask`
  stays float 0/1 and `.to(xq.dtype)` hands SDPA a tensor it treats as an **additive bias**:
  padded positions get `+0.0` (fully attended), real positions get `+1.0`.
  Probe: mutating masked-off tokens moved next-token logits by **1.02**.
  The `seq_len>1` path is correct (verified diff `0.0`), so this is generation-only — it hits
  every batched/left-padded rollout (`rollout_engine.py`, `train_grpo.py`, `train_ppo.py`,
  `scripts/serve_openai_api.py`).
  **Fixed** (`7b22b7b`): the mask is now built as a single-layer *bool* keep-mask and
  only then tiled across the layer dimension, so SDPA can never reinterpret it as a bias.
  Covered by `test/test_bugfix_regressions.py::TestDenseAttention`.

- [x] **C3 — Engram breaks its own batch↔incremental invariant in stage 2** — `:240`
  `ShortConv` is causal but **stateless**. With `kernel_size=4, dilation=max_ngram_size=3` it has a
  10-token receptive field; at `seq_len=1` it convolves against zero padding.
  Probe: stage-1 features identical, **stage-2 max abs diff `2.99e-01`**.
  This is exactly the invariant `readme_engram.md` ("Inference Consistency") claims to hold.
  `test/test_engram_v2.py` only covers stage 1, so it passes.
  **Fixed**: `ShortConv` now runs *before* gating, so its input is a pure function of the token
  history; `stage1_gather` takes an `n_context` argument and pre-fetches
  `conv_context = (kernel_size-1)*max_ngram_size` extra history rows so the convolution window is
  identical in both paths. Covered by `test/test_bugfix_regressions.py::TestEngram`.
  **Note:** this reorders gate/conv (`gate * (v + conv(v))` instead of `g*v + conv(g*v)`), which
  changes numerics for existing engram checkpoints — they were already inconsistent at inference,
  so a re-train is needed either way.

- [ ] **C4 — `use_recurrence` + KV cache double-counts memory** — `:371-386`
  `c = cat(mems, x)` recomputes K/V for mem positions already present in `past_key_value`.
  Probe: a 1-token decode step grew the cache **6 → 13**. `start_pos` drifts by `mem_len+1` per
  step, so `generate()`'s `input_ids[:, past_len:]` slice goes empty.
  `generate()` with `use_recurrence=1` cannot work.

- [ ] **C5 — All mem tokens collapse onto RoPE position 0** — `:578-587`
  In training `start_pos==0`, so `k_start=-mem_len` and the entire mem block is padded with
  `freqs_cos[:1].repeat(...)`. Probe: `k_len=12` but only **6 distinct positions** — all 6 mem
  tokens share position 0, which is *also* the first current token's position.
  Transformer-XL relative positioning is destroyed.

---

## High

- [ ] **H1 — Mems harvested from the wrong tensor** *(inspection)* — `:535`, `:631-636`
  `MiniMindBlock.forward` reassigns `normed_x` at `:530`, so the returned value is
  `post_attention_layernorm(hidden)`. It is consumed at `:372` concatenated with
  `input_layernorm(hidden)` — two different norm spaces, and it is layer *i*'s post-MLP-norm
  state rather than its input hidden state as TXL specifies.

- [ ] **H2 — `mems` carried across shuffled batches** — `trainer/train_pretrain.py:191-194`,
  `trainer/train_reasoning.py:27-47`
  `indices = torch.randperm(len(train_ds))` feeds `SkipBatchSampler`, then `mems = res.next_mems`
  is threaded batch-to-batch. Consecutive "segments" are unrelated random documents.

- [x] **H3 — `global_kv_pool` never reset per loop** — `:599`, `:606-607`
  With `use_looped_transformer`, key lengths per attention call (3 layers × 3 loops, seq=8) were
  `[8,16,24,32,40,48,56,64,72]`. Loop 2 layer 0 attends to loop 1's KV; memory is quadratic in
  `layers*num_loops`; violates the "layers 1..L" invariant in `readme_dense_attention.md` §1.1.
  **Fixed**: the pool is rebuilt at the top of each loop iteration, so key lengths are now
  `[8,16,24]*3` instead of `[8..72]`. Covered by
  `test/test_bugfix_regressions.py::TestLoopedTransformer`, which also pins the non-looped
  behaviour and the looped KV-cache index mapping.

- [x] **H4 — `max_ngram_size > 3` throws** — `:163`, `:196-203`
  `multipliers` is a hardcoded 3-element buffer → `IndexError: index 3 is out of bounds`,
  despite the config advertising `max_ngram_size` as tunable.
  **Fixed**: multipliers are generated to length `max_ngram_size` (first three values unchanged,
  so `max_ngram_size=3` checkpoints are numerically identical); buffer is now `persistent=False`
  since it is a deterministic constant; `max_ngram_size < 2` now raises instead of dividing by
  zero via `total_heads == 0`. Overflow headroom verified up to `max_ngram_size=8`.

- [ ] **H5 — Engram offload puts parameters on mixed devices** — `:176-180`
  After `.cuda()`, `{p.device for p in model.parameters()} == {'cpu', 'cuda:0'}`.
  `DistributedDataParallel(model, device_ids=[local_rank])` (`train_pretrain.py:186`) rejects that.
  Also `super()._apply(fn)` moves the full table *to* GPU before `.cpu()` pulls it back — a
  transient VRAM spike that defeats the point of offloading.

- [ ] **H6 — MoE router gets no gradient under current defaults** — `:469-470`
  Defaults are `num_experts_per_tok=1, norm_topk_prob=True`, so `topk_weight/topk_weight.sum()`
  is exactly `1.0` and the LM loss cannot reach `self.gate`.

  | k | norm_topk_prob | gate grad norm |
  |---|----------------|----------------|
  | 1 | True           | **1.09e-07**   |
  | 1 | False          | 5.46e-01       |
  | 2 | True           | 9.19e-01       |

  Relevant because `HEAD` is `[Update] change moe default`.

- [ ] **H7 — Non-flash path + mems crashes on any attention_mask** — `:437-438`
  `attention_mask` has length `seq_len` but `scores` has `mem_len+seq_len` keys
  → `RuntimeError: size of tensor a (12) must match b (6)`.

---

## Medium / performance

- [x] **M1 — Dense mask assumes `attention_mask.shape[-1] == total_seq_len`** *(inspection)* — `:409`
  `am.repeat(1,1,1,depth)` misaligns whenever mems are active.
  **Fixed** alongside C2: the mask is left-padded with `True` to `total_seq_len` before tiling.

- [ ] **M2 — Dense pool stores post-`repeat_kv` K/V** — `:395-396`
  Holds `n_rep`× more than needed; erases GQA's cache savings.

- [ ] **M3 — Attention dropout silently disabled under dense** — `:422-426`
  The dense branch never passes `dropout_p`.

- [ ] **M4 — Engram re-hashes the entire prefix every decode step** — `:182-226`
  `(hashed_len, needed_len)` = `(16,16), (17,1), (18,1), (19,1), (20,1), (21,1)`.
  Quadratic; with `offload_cpu` it round-trips the full prefix over PCIe per step for one row.

- [ ] **M5 — `aux_loss` loses all but the last loop; `load` has the wrong shape** — `:641`, `:481`
  `aux_loss` is read off `l.mlp.aux_loss` after the fact, so with `num_loops>1` only the last
  loop's value survives. `load = one_hot(topk_idx,E).float().mean(0)` is `[k,E]`, not `[E]`;
  the `.sum()` double-counts across slots for `k>1`.

- [ ] **M6 — Per-layer, per-step GPU→CPU sync** — `:430`
  `torch.all(attention_mask == 1)` in the flash-path guard forces a device sync.

- [ ] **M7 — `labels` + `logits_to_keep` misaligned** — `:653-661`
  Logits sliced to the tail, labels still shifted from position 0 → `ValueError`.
  Not currently hit (`rollout_engine.py` uses `logits_to_keep` without `labels`).

---

## Test-coverage gaps that let these through

- [ ] **T1** — `test/test_engram_v2.py` stops at `stage1_gather`; a `stage2_fusion` equivalence
  assertion catches **C3** immediately.
- [ ] **T2** — `test/test_dense_attention.py` asserts nothing and never exercises
  `attention_mask` or `use_cache`; **C2** is invisible to it.
- [ ] **T3** — `test/test_looped_transformer.py` only counts `len(presents)`; it does not check
  KV-pool shapes, so **H3** passes.
- [ ] **T4** — No test at all for `use_latent_attention` or `use_recurrence`
  (hence **C1, C4, C5, H7**).

---

## Operational hazard

- [ ] **O1 — `eval_llm.py` exposes no `--use_recurrence`, `--mem_len`, `--kv_lora_rank`,
  `--qk_rope_dim`, or engram-shape flags.**
  Because loading is `strict=False` (see `CLAUDE.md`), checkpoints trained with non-default
  values for those load silently wrong rather than erroring.
