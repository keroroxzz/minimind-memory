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

**Reasoning depth — the ceiling, measured on a task that actually requires depth.**
The depth task went through three versions; the first two were both invalid, and their
conclusions have been retracted:

| task | non-commutative? | entropy preserved? | verdict |
|---|---|---|---|
| mixed arithmetic (`+-*`) | yes | **no** — 3.08 → 0.62 bit | long chains reward guessing; multiply-only accuracy *rose* with depth to 87% |
| add/sub only | **no** — collapses to `v0 + Σ±rhs` | yes | a sum is one attention layer (TC⁰); 8 layers scored 90% at k=12 |
| **S₅ permutation composition** | yes | yes (bijections preserve uniformity) | **current** |

Permutation composition is the right instrument: S₅'s word problem is NC¹-complete, so a
fixed-depth transformer (≈TC⁰) provably cannot solve arbitrary lengths, whereas addition sits
inside TC⁰ — which is exactly why the add/sub version showed no ceiling.

Measured on S₅ composition, vanilla 29M / 8 layers (6k steps, exact-match chance 1/120 = 0.8%,
per-position chance 20%):

| k | 1 | 2 | 3 | 4 | 5 | 6 | 8 | 12 |
|---|---|---|---|---|---|---|---|---|
| exact | 99.3% | 96.7% | **62.7%** | **11.3%** | 1.3% | 0.0% | 0.7% | 0.7% |
| per-position | 99.7% | 98.9% | 82.8% | 44.0% | 28.0% | 22.5% | 19.5% | 18.9% |

**The ceiling is k≈3, and by k=5 it is at chance.** Roughly 2.5 layers per composition step.
The sensitive window for any depth-adding mechanism is **k=2..5**.

**Retracted (2026-07-31):** two earlier claims in this file were artefacts of the broken tasks.
"Ceiling at k≈8, matching the layer count" came from the mixed-arithmetic task plus a
distribution shift, and "carry propagation / state accumulation is the real bottleneck" came from
the tens digit being hard *because multi-digit multiplication is hard* — on add/sub the tens digit
went straight to ~100%. Neither says anything about depth or state.

**A data-design trap worth remembering.** Holding out val by using a disjoint start-value range
(train 10–79 / val 80–99) is safe under mixed arithmetic, because multiplication scatters values
across the whole range within two steps. Under add/sub-only it is fatal: the answer stays pinned
near `v0`, so the model memorises the training band and scores **100% in-range but 0% out-of-range**
while training loss reaches 0.02. Hold out by excluding specific examples, not by partitioning the
value space, unless you have checked that the dynamics mix.

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

**`use_looped_transformer` buys reasoning depth super-linearly — the strongest result here.**
S₅ composition, k=1..24, 12000 steps, all four from one shared checkpoint, and loop2/3/4 carrying
*identical* parameter counts (29.50M) so only compute differs:

| | steps | overall | ceiling k* | k*/steps | VRAM |
|---|---|---|---|---|---|
| loop1 | 8 | 10.3% | 2.69 | 0.34 | 2.54 G |
| loop2 | 16 | 20.1% | 4.83 | 0.30 | 4.18 G |
| loop3 | 24 | 73.2% | **17.92** | **0.75** | 5.84 G |
| loop4 | 32 | **99.1%** | **>24** | >0.75 | 7.50 G |

Not linear — super-linear, and not smooth: loop2→loop3 is 1.5× the compute for 3.7× the ceiling,
with `k*/steps` jumping 0.30 → 0.75. This looks like a phase transition in strategy: at ≤2 loops
the model appears to use a shallow lookup, at ≥3 loops something close to the actual iterative
algorithm (loop3 degrades right where its 24 steps run out, at k≈18).

**+0.9% parameters bought >9× reasoning depth**, at 3× VRAM. This is the mechanism the thesis
needs; CompleteAttention does the opposite.

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
- **Token count is a property of the string *in its context*, not of the string.** `'q'` is one
  token alone but two with a leading space, so a 5-letter code containing `q` became 6 tokens —
  reintroducing target-length variation into the very condition built to control it, and
  correlating it with specific codes. When a task depends on targets being a fixed length, verify
  the *full* target fragment as it actually appears (leading space, boundaries), never the bare
  character. `'? ? ? ? ?'` is 9 tokens for the same reason — the space is its own token.
- **Never let a dataset builder skip rows silently.** `synth_depth_task.py` used to
  `continue` past anything longer than `SEQ_LEN`. That does not thin a dataset uniformly — it
  removes the *deepest* samples first, exactly the end of the range a depth experiment measures,
  and the only visible symptom is a smaller total. It now counts drops per k and raises. A test
  run at the old limit dropped 150 rows, every one of them at k=22–24. Any change that lengthens
  a prompt must re-check the limit.
- **A matched comparison needs a fingerprint, not a shared seed.** Three conditions built from
  "the same seed" are only argued to be identical. `make_n2` emits a canonical
  `(defs, state, chain, answer)` latent that never reaches the model, and the SHA of that stream
  is stored in `train_dist`. Hashing `(k, answer)` is *not* enough — S₅ has 120 answers, so it is
  a many-to-one summary. Check the fingerprint changes with seed too: a hash that is accidentally
  constant also "matches" across conditions.
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
