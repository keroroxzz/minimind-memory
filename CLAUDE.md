# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Research thesis (read this first)

This is a research fork of MiniMind (a from-scratch, no-`transformers`/`trl`/`peft`-abstractions
small LLM + full training pipeline). It is **not** trying to reproduce the upstream release.

The thesis being tested is that **model capability decomposes into reasoning × knowledge**, and
that the knowledge half can be externalised into a separable, swappable module. If that holds, a
small backbone only needs to be good at *reasoning*, and can "punch above its weight class"
(越級打怪) — matching 7B/12B/27B models on logic while carrying almost none of their parameters.

Two axes, and the price of each:

| axis | mechanism | what it costs |
|---|---|---|
| **reasoning depth** | `use_looped_transformer`, `use_dense_attention`, `use_recurrence` | parameters: ~0 · compute: varies wildly (see below) |
| **externalised knowledge** | `use_engram` | parameters: +105M · VRAM: ~0 if offloaded · speed: 6.2× slower if offloaded |
| **making the above affordable** | `use_latent_attention` | KV cache: −25% vs GQA · parameters: slightly negative |

"Price" is a first-class research question here, not an afterthought. A mechanism that buys
reasoning depth at 5× the compute has not made a small model competitive — it has just moved the
cost. Every claim in this repo should carry its cost accounting.

`readme_dense_attention.md` and `readme_engram.md` describe the two big mechanisms, but **both
documents contain framing errors that measurement has since contradicted** — see "Measured
findings" below before trusting them.

Most CLI scripts print help text and comments in Chinese; code identifiers are English.

## Measured findings (2026-07-31 — do not re-derive these)

All from `experiments/`, single seed, 29M backbone (512d / 8 layers / 8 heads / 2 KV heads).

**Reasoning depth — the positive result, and its ceiling.** On a depth-controlled synthetic task
(k-step dependency chains, answer-only target so autoregression cannot supply depth), a vanilla
29M model composes in Z₁₀ inside a single forward pass out to real depth. Measured ones-digit
accuracy, trained on k=1..16:

| k | 1 | 2 | 3 | 4 | 5 | 6 | 8 | 10 | 12 | 16 |
|---|---|---|---|---|---|---|---|---|---|---|
| acc | 99.5% | 97.5% | 92.5% | 91.0% | 80.5% | 72.5% | 56.5% | 45.5% | 44.5% | 42.0% |

The ~42% tail plateau is **confirmed to be a distributional shortcut, not composition.**
Splitting held-out chains by operation mix (150 each, start values unseen in training):

| k | add/sub only | mixed | multiply only |
|---|---|---|---|
| 4 | 100.0% | 91.3% | 76.0% |
| 8 | 47.3% | 55.3% | 84.0% |
| 12 | 18.7% | 40.7% | 88.7% |
| 16 | **12.7%** | 36.7% | **87.3%** |

Multiply-only accuracy *rises* with depth, which is impossible for genuine computation. Cause:
repeated ×2..9 mod 10 collapses the answer's entropy from 3.08 bit at k=1 to **0.62 bit at k=16**
(one value covers 91% of cases), so guessing beats computing. Add/sub-only chains, whose range is
never compressed, decay to 12.7% — essentially the 10% chance level.

**Implication for experiment design: the depth task must use add/sub-only chains.** With
multiplication in the mix, an architecture can "win" by exploiting the shortcut rather than by
reasoning deeper. On add/sub-only the true sensitive window is **k=6..10** (94.0% → 47.3% →
24.7%), which is where any depth-adding mechanism should be measured.

**Caveat:** an earlier run trained only on k=1..6 reached 93.3% at k=6 versus 72.5% here. The
number depends on the training difficulty distribution, not on the architecture alone — do not
quote a single-depth accuracy without stating the training range.

**The real bottleneck is state, not depth.** On the same task the *tens* digit — which requires
carry propagation, i.e. a small piece of state accumulated across steps — sits near the 10%
per-digit baseline at every k, while the *ones* digit is ~99%. Attention rebuilds state from
scratch each layer; this is where recurrence/SSM-style mechanisms should be aimed.

**Complete Attention (`use_dense_attention`) does not buy reasoning depth — it hurts.**
Ones-digit accuracy at k=6: **93.3% vanilla vs 54.0% dense**, with the gap *widening* with depth.
Conceptually, cross-layer attention is a dense *skip connection*: it shortens effective path
length rather than adding sequential computation, so it is aimed at the wrong quantity. It also
costs 1.65× wall-clock at seq 512, and the FLOP multiplier is
`1 + attn_share × ((N+1)/2 − 1)` — 1.49× at 8 layers/seq 512, but **~5.5× at 24 layers/seq 2048**.
It scales badly in both depth and context.

**Engram did not beat vanilla on pretrain perplexity** (3.34 vs 3.28 at 246M tokens) despite
4.6× the parameters. But note that pretrain perplexity mostly rewards memorisation — the axis
engram is supposed to *offload* — so this is weak evidence either way.

**`use_looped_transformer` is the mechanism that actually adds sequential computation**
(`num_loops` × layers effective steps for ~0.5M extra parameters) and **has never been
evaluated**. It is the most promising untested item in the repo.

Pretrain reference numbers (246M tokens, 30k steps, bs16, seq512): vanilla val_loss 1.696
(ppl 5.45), 40.6 min, 3.25 GiB peak on an RTX 4070.

## Environment setup

Use the existing conda environment `sd` (activate it rather than creating a venv or reinstalling
from `requirements.txt`):

```bash
conda activate sd
```

Trainers assume execution from inside `trainer/`; they use `sys.path.append(...'..')` plus
`__package__` hacks to import sibling packages. Keep that pattern for new entry points.

Datasets (`.jsonl`) go in `./dataset/`; `_mini` variants are the fast-iteration versions.

**Disk:** `/` is chronically ~99% full. `TMPDIR` defaults to `/tmp` on that partition, so pip and
anything else needing scratch space will fail with `No space left on device`. Redirect per-command
(`TMPDIR=/home/rtu/tmp-xyz pip install ...`) — `/home` is on an HDD with room, and
`/media/rtu/storage` is the SSD where the conda env actually lives.

## Experiments

`experiments/` holds the controlled architecture comparisons. These are the primary research
surface now — the trainers in `trainer/` are upstream infrastructure.

```bash
python experiments/prepare_data.py            # pretrain corpus -> packed int16 tensor
python experiments/run_ablation.py            # 4-way pretrain comparison, saves checkpoints
python experiments/plot_results.py

python experiments/prepare_reasoning_data.py  # Belle Chinese math CoT -> masked SFT tensor
python experiments/run_reasoning_sft.py       # SFT from ONE shared checkpoint, flags switched
python experiments/eval_reasoning_quant.py    # CoT loss + exact-match w/ Wilson CIs

python experiments/synth_depth_task.py --gen --max-k 16
python experiments/synth_depth_task.py --run vanilla --max-k 16 --throttle 0.4
python experiments/synth_depth_task.py --report
```

Design rules these encode, which matter more than the code:

- **Compare from one shared pretrain checkpoint.** `use_dense_attention` adds zero parameters and
  loads into a vanilla state dict with zero randomly-initialised weights (verified), so
  vanilla-vs-dense isolates the connectivity change exactly. `use_engram` adds 105M random
  parameters; `use_latent_attention` cannot load at all (`o_proj` is 512×512 vs 512×128).
- **The depth task's target is the final answer only, never intermediate steps.** These
  mechanisms buy depth *within one forward pass*; allowing chain-of-thought lets autoregression
  supply unlimited depth and erases the architectural difference.
- **Split accuracy by digit.** Aggregate accuracy hid the entire result — ones digit (shallow Z₁₀
  chain) and tens digit (carry propagation) behave completely differently.
- **`--throttle 0.4`** duty-cycles the GPU via sleeps (62W instead of 175W, 43°C instead of 65°C,
  fan silent). `nvidia-smi -pl` would need sudo, which is not available here.

## Experiment gotchas that have already cost time

- **`use_engram` defaults to `True` in `MiniMindConfig`.** A "vanilla" config that omits the flag
  silently runs *with* engram (134M params instead of 29M). Pin every flag explicitly.
- **The 6400 BPE vocab tokenises numbers inconsistently** — 42 of 0..99 are one token, 58 are two
  (`'60'→[3873]` but `'97'→[60,58]`). This crippled arithmetic: formatting numbers as
  space-separated digits took the depth task from 3.7% → 11.3% at 60% fewer steps.
- **Weight decay must be grouped.** AdamW decays every parameter each step, but only ~0.4% of the
  105M-row engram table receives gradient — uniform decay drags unvisited rows toward zero.
  Exempt embeddings, norms and the engram table.
- **Answer extraction by "last number in the text" is unreliable on Chinese math.** Solutions
  routinely end with a remainder or a multi-part answer; manual inspection found 1 of 4 correct.
  Use the synthetic task (answer determined by construction) for accuracy claims.
- **`eval_reasoning.py` is not a quantitative eval** — four hard-coded questions streamed for a
  human to read. Use `experiments/eval_reasoning_quant.py`.
- **`ReasoningDataset` does not mask padding in its labels**, so the model is trained to emit pad;
  with a ~90-token median response in a 512 window most supervised positions are padding.
  `experiments/prepare_reasoning_data.py` does not reuse it.

## Common commands

### Inference / chat

```bash
python eval_llm.py --weight full_sft            # torch-native ./out/<weight>_<hidden>[...].pth
python eval_llm.py --load_from ./minimind-3     # transformers-format directory
cd scripts && streamlit run web_demo.py
python scripts/serve_openai_api.py --weight full_sft
```

`eval_llm.py` takes the same architecture flags as training, and they **must match the
checkpoint** — the model is built from the config before weights load. Loading is `strict=False`,
so a mismatch means those modules stay randomly initialised. `eval_llm.py` now prints a warning
naming the skipped parameters; `eval_reasoning.py` and `serve_openai_api.py` still fail silently.

### Training

```bash
cd trainer
python train_pretrain.py                 # stage 1, required
python train_full_sft.py                 # stage 2, required
python train_reasoning.py                # thinking-chain SFT; defaults use_dense_attention=1
python train_lora.py --lora_name lora_medical
python train_dpo.py / train_grpo.py / train_ppo.py / train_agent.py / train_distillation.py
torchrun --nproc_per_node N train_xxx.py # DDP, same node
```

Common flags: `--data_path`, `--from_weight`, `--save_weight`, `--epochs`, `--batch_size`,
`--accumulation_steps`, `--hidden_size`/`--num_hidden_layers`, and the architecture toggles.
`--from_resume 1` auto-resumes from `./checkpoints/<name>_resume.pth` (model + optimizer + step +
wandb id, rescaling `step` on GPU-count change). Final weights land in
`./out/<save_weight>_<hidden_size>[_moe][_engram].pth` (`trainer_utils.py::get_model_paths`).

### Tests

No pytest config — run directly as scripts:

```bash
python test/test_bugfix_regressions.py  # 49 tests, the main regression suite
python test/test_engram_v2.py           # engram hashing + stage-2 equivalence
python test/test_dense_attention.py     # pool depth, causality, mask/cache behaviour
python test/test_looped_transformer.py  # per-loop pool reset, LoopLoRA init, cache round-trip
```

`test_bugfix_regressions.py` is keyed to `FIX_TODO.md` — each test names the finding it pins.
Add to it rather than writing ad hoc scripts.

## Architecture

Everything lives in `model/model_minimind.py`, gated by `MiniMindConfig` flags.

### Reasoning-depth axis

- **`use_looped_transformer`** — runs the same layer stack `num_loops` times with small per-loop
  LoRA adapters (`LoopLoRA`, rank `loop_lora_rank`, B initialised to zero so extra loops start as
  a no-op). `num_loops` × layers sequential steps for ~0.5M parameters. **The mechanism most
  aligned with the thesis, and still unevaluated.**
- **`use_dense_attention`** (`readme_dense_attention.md`) — cross-layer KV pooling: layer *L*'s
  query attends over the KV of layers `1..L`, via a `global_kv_pool` rebuilt each forward with a
  bool causal mask tiled across the layer dimension. The pool resets per loop iteration. Measured
  to *hurt* reasoning depth (see above); treat §1.1's framing as unsupported.
- **`use_recurrence`** — Transformer-XL segment recurrence; per-layer `mems` (detached,
  capped at `mem_len`) concatenated onto K/V. Ignored when a KV cache is present, since the cache
  already holds that history. **Currently inert in training**: every `PretrainDataset` row is a
  standalone document, so there is no contiguous segment stream to recur over. Making it real
  needs a dataset that emits ordered sub-segments.

### Knowledge axis

- **`use_engram`** (default **on**; `readme_engram.md`) — a deterministic n-gram associative
  memory. **Stage 1** (`stage1_gather`) hashes recent n-grams (XOR-mixed rolling hashes, one prime
  modulus per head) and looks up a large table, optionally CPU-resident (`engram_offload_cpu`) so
  millions of rows cost no VRAM. **Stage 2** (`stage2_fusion`) smooths with a `ShortConv` and then
  gates the result into `engram_layers` by dot product with the hidden state.
  The invariant (`test_engram_v2.py`): incremental and full-batch forward must produce identical
  features at the same position, which is why stage 1 pre-fetches `conv_context` extra history.
  **Design limitation worth knowing:** it is keyed by *surface n-grams*, not by the model's
  current hidden state, and injected additively. It is a static associative table, not the
  query-conditioned dynamic memory the thesis calls for — RETRO-style chunked cross-attention is
  the closer reference point.

### Cost-reduction axis

- **`use_latent_attention`** (`readme_dense_attention.md` §1.2) — DeepSeek-MLA-style low-rank KV
  compression: content compressed to `kv_lora_rank` and split per head, K and V sharing one
  latent, with a small shared `qk_rope_dim` carrying RoPE. The cache stores **only the compressed
  latent** (`kv_lora_rank + qk_rope_dim`), expanding on read. 192 values/token/layer vs GQA's 256.
  The saving is config-dependent: it holds only when
  `kv_lora_rank + qk_rope_dim < 2 × n_kv_heads × head_dim`.
  **§1.2's stated premise is wrong** — it justifies latent attention as offsetting "the KV cache
  blow-up that dense attention causes", but dense attention's cache is *identical* to vanilla's
  (256 both); the pool is rebuilt per forward and stores nothing extra. Dense inflates attention
  compute and activation memory, not the cache.
- **`use_moe`** — top-k routed experts with an aux load-balance loss. Orthogonal to the above.
  Note `norm_topk_prob` is skipped at `k=1`, where normalising makes the weight exactly 1.0 and
  severs the router's gradient path entirely.
- **YaRN RoPE scaling** (`inference_rope_scaling`) — inference-time extrapolation in
  `precompute_freqs_cis`.

### Invariants to preserve

`MiniMindForCausalLM.generate` is a custom autoregressive loop, needed because the per-layer
KV/mems/engram plumbing does not fit the HF cache interface. **Any change to attention or engram
must be checked against both `forward` (batched) and `generate` (incremental)** — keeping those
equivalent is the hard part of this design, and historically every serious bug in this repo lived
on the incremental path while the training path was fine. `test_bugfix_regressions.py` pins the
equivalences.

When adding a mechanism: a config flag with a default in `MiniMindConfig.__init__`, logic branched
on that flag inside `Attention`/`MiniMindBlock`/`MiniMindModel`, a regression test, and a cost
measurement (params, VRAM, tokens/s) alongside any quality claim.

`model/model_lora.py` implements LoRA from scratch, not via `peft`.

### Supporting infrastructure

Every `train_*.py` follows: argparse → `MiniMindConfig` → `trainer_utils.init_model` → dataset
from `dataset/lm_dataset.py` or `dataset/data_reasoning.py` → optional DDP → train loop with grad
accumulation and periodic `lm_checkpoint`.

`trainer/trainer_utils.py` is the shared core: `get_model_paths`, atomic `lm_checkpoint` saves,
`init_distributed_mode`, `SkipBatchSampler` for mid-epoch resume, plus `set_ddp_ignore` and
`sync_offloaded_grads` — the latter two exist because the CPU-offloaded engram table cannot be
DDP-managed (mixed devices) and so needs its gradient all-reduced manually over a gloo sub-group.

RLAIF/agentic RL decouple rollouts via `trainer/rollout_engine.py` (`torch` in-process or
`sglang` over HTTP, `--rollout_engine`).

### Observability

Trainers `import wandb` but the project routes through SwanLab (API-compatible drop-in, since
WandB is often unreachable from mainland China). `--use_wandb` opts in.

### Tokenizer

Custom `minimind_tokenizer` (BPE + ByteLevel, vocab 6400, intentionally small so the
embedding/output layers do not dominate a model this size), with `<tool_call>`, `<tool_response>`
and `<think>` tokens via `chat_template.jinja`. Do not retrain or change it casually — vocab
changes break weights, data formats, and llama.cpp/vllm/ollama compatibility. Be aware of its
inconsistent number tokenisation when designing arithmetic or reasoning tasks (see gotchas).
