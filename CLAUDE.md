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

## C layer (memory module) — G1 status, 2026-08-04

`design_c_layer.md` is the spec; `research.md` §4.23–4.24 has the numbers. Code lives in
`model/memory_module.py`, `experiments/g1_renderer.py`, `experiments/g1_train.py`, with
invariants in `test/test_memory_module.py` (58 checks, run it directly).

**The task**: a frozen core trained on explicit values (`L0`, 100%) must instead consume a
25-dim lossless latent delivered by a learned adapter. Selection is oracle and the store is
frozen, so only delivery is under test — a failure localises.

| delivery | where | params | overall |
|---|---|---|---|
| `oracle_inline` / `teacher_kv` (native content, same position) | in-place | **0** | **100%** |
| `InlineLatent` (latent → embedding) | in-place | 1.33M | **100%** |
| `contextual` (latent + native KV → **KV delta**) | in-place | 1.13M | **100%** |
| `zdelta` (latent → **KV delta**, no context) | in-place | 1.13M | **99.0%** |
| `static-full` (latent → **absolute** KV, 16 heads) | in-place | 6.32M | 37.2% |
| `SyntheticKV` (latent → absolute KV) | **prefix** | 1.06M | 34.8% |
| `LatentSlots` (latent → carrier token) | **prefix** | 0.14M | 3.3% |
| none | — | — | 1.2% |

**What holds**: a frozen core can consume learned latent delivery at 100%, delivered as an
embedding at the value positions or as a residual KV correction. Delivery is an *embedding or
KV*, not text — "memory must enter as tokens" is false. Capacity is not the bottleneck:
6.32M absolute fails where 1.13M residual succeeds.

**What does not hold** — retracted claims, do not resurrect:
- "delivery must be JIT/streaming" (`padded k≤4` = 100%)
- "memory must flow through the backbone" (`zdelta` injects per-layer and scores 99.0%)
- "the synthesiser must see the layer state" (`zdelta` has no context input)
- "online context is unnecessary" — the *correction* needs none, but the native K/V scaffold
  is still computed online at those positions
- residual-vs-absolute is the leading factor but **not yet a necessity**: the failing absolute
  cells differ in parameters and architecture. An exact-matched `zabs` decides it.

**Cost**: `f(z)` is offline-computable after retrieval; the carrier positions' native forward
and the per-position addition are online. Not the old prefix-prefill model.

**Milestones** (never backfill): pre-registered `L1` = FAIL (both prefix paths);
`L1-inline` = pass (diagnostic); `L1-v2` (in-place KV) = PASS.

## C layer — G2 status, 2026-08-04

`research.md` §4.26–4.30. Retrieve was tested in three stages, all with the core and `zdelta`
frozen, so only the retriever is under test.

| stage | address source | key identities | result |
|---|---|---|---|
| G2a | fixed random orthogonal | fixed `f0..f3` | **100%** ordered / per-step / hit-miss |
| G2b | same, query from frozen core hidden | fixed | **100%** |
| G2c | **tied encoder over canonical key spans** | **train/cal/test disjoint** | retrieval **98.1%**, hit/miss **73.3% / 81.5%** |

**G2c splits.** Unseen-identity *ranking* transfers (per-step recall 98.1% on both seeds, and
the encoder pushes input-side max|cos| 0.964 apart to 0.824). A single support **threshold does
not** transfer: calibration reads 99.3–99.5%, untouched test reads 73.3–81.5%. Diagnosis
(`g2c_diagnose_support.py`) shows the *score* separates — an oracle threshold reaches 96.3–98.0%
— so the gap is entirely in threshold placement.

**G2c-cal-v2 = FAIL, permanently.** A pre-registered single re-calibration on 40 brand-new
identities failed on both seeds. The utility gate is what caught it: subject to a one-sided 95%
Clopper–Pearson bound on hallucination ≤5%, the *minimum achievable* false-abstain on the
calibration set was already 98%. Without that gate this would have passed looking like
"R_abstain 100%, halluc 0%" — a threshold that always abstains. Do not reopen: no third version,
no length-conditioned threshold, no rebalanced retraining.

Confound that narrows the wording but does not change the verdict: the v2 identity sets are
almost all 3-token spans while training identities are almost all 2-token, so the failure is
under a **joint identity + span-structure shift** and this design cannot separate the two.

**Design debt recorded, not paid here**: a canonical encoder's training must cover the
tokenization structure it will meet; support scores must be robust to structural shift.

## C layer — G3a write formation, 2026-08-04

`research.md` §4.31–4.33. Frozen core + `zdelta`, oracle retrieval, `missing=0`, the writer the
only learnable part. The generalisation axis is the *permutation* (95 train / 24 val, disjoint),
not key identity. Four conditions are mandatory: `oracle` (ceiling), `writer`, `zero` (floor),
`shuffled` (writer reads a different entry's event — if that also scores, the result is void).

| version | writer output | end-to-end (unseen perms) | row argmax | valid perm | latent dist |
|---|---|---|---|---|---|
| oracle | `perm_to_latent` | **99.0%** | — | — | 0 |
| **v1** | unconstrained 25-d | **3.5% — FAIL** | 37.1% | 0.0% | 2.32 |
| **v2** | **5×5 row-softmax** | **99.0% — PASS** | **100%** | **100%** | **0.001** |
| zero / shuffled | — | 0.5% / 0.8% | — | — | — |

v2's writer matches the oracle cell for cell. It reconstructs `perm_to_latent` to within 0.001
having never been supervised on that encoding — only downstream answer loss.

**Only end-to-end is not enough to read this.** v1 scored 37.1% row argmax against 20% chance —
partial formation invisible in a 3.5% end-to-end number. `g3a_formation_metrics.py` reports
row argmax / exact perm / valid-permutation rate / row entropy / latent distance; run it on any
writer checkpoint.

**What may be claimed**: a row-categorical parameterisation makes task-loss-only write formation
*optimisable* — a writer that has never seen `zdelta` can form, from answer gradient alone, a
latent that a frozen delivery interface which has never seen the writer consumes exactly, and
this generalises to unseen permutations. Row-softmax guarantees only that each row is
non-negative and sums to 1; the 100% valid-permutation rate and 0.005 entropy are *learned*, not
structural.

**What may not**: do not attribute this to manifold mismatch alone — v1→v2 changed the output
constraint *and* the gradient geometry/scale, so the credit belongs to the structure/conditioning
bundle. Do not say it "emerges without priors": the conclusion is scoped to the known S₅ latent
schema, not a general latent writer. `G3a-v3` (reconstruction scaffold) was conditional on v2
failing and is therefore **not run** — adding auxiliary labels would downgrade the milestone to
supervised formation.

**Span-structure fragility is cross-cutting, not a G2c quirk.** The G3a writer generalises
*perfectly* to unseen keys — but only to 2-token ones. `f4..f11` (all 2-token) score 100% exact
permutation; `f41..f44/f46/f47` (all 3-token) score 58–96%, because the extra key token pushes
the value span from index 5 to 6. G2c-cal-v2 failed under a shift of the same
*kind*, but **the mechanism is not proven to be the same** — G2c's failing path was an
order-aware embedding encoder plus support calibration, the writer's is event→latent, and G2c's
primary never relied on core hidden. The shared, defensible statement is narrower: both are
fragile to **tokenizer-induced span-length/position shift**. Do not elevate it to a theorem about
frozen cores. Of `f0..f47`, 32 keys are 2-token and 16 are 3-token.

## C layer — G3b closure, 2026-08-04

`research.md` §4.34. All four components loaded frozen, **zero training**, wired as
`event → writer → store.commit → retrieve → store.read → delivery → answer`, 400 episodes.

**Scope — state it, do not generalise it**: only **4 query identities** (`f0..f3`, the set G2b
actually trained queries on) and at most **32 orthogonal addresses** (`address_vector` draws from
`address_bank`, so `f32`+ has no address at all — G2b never saw those either). Pool 8, 29
distractors, permutations from G3a's unseen val split, `p_omit` 14.8%. Address still comes from
the known key rule, so **write-address formation remains untested**. Each key written at most
once, store cleared per episode: overwrite/reconsolidation is deliberately excluded.

| chain step | primary (2-token) | stress (3-token, exploratory) |
|---|---|---|
| 1. writer formation exact (unconditional) | **100.0%** (n=904) | 95.7% (n=897) |
| 2. address retrieval exact | **99.6%** (n=1000) | **7.9%** (n=1000) |
| 3. content fidelity \| address correct | **100.0%** (n=904) | — (n=0) |
| 4. executor \| address+content correct | **98.2%** (n=341) | — (n=0) |
| 5. **end-to-end** | **98.2%** (n=341) | **0.0%** (n=339) |
| R_abstain / halluc / false_abstain | 94.9% / 5.1% / 0.0% | 90.2% / 9.8% / 85.5% |

**Integration costs nothing.** Every cell of the 2×2 intervention
(`oracle|learned writer × oracle|learned retrieval`) scores **98.2%** end-to-end — including
`oracle × oracle`, so the missing 1.8% is the delivery/executor itself (G1's `zdelta` ceiling is
99.0%), not any learned component. Direct delivery and store round-trip are also identical, so
**the store round-trip is free**. Abstention is the only cell that moves (oracle retrieval 100%
vs learned 94.9%), which places the 5.1% hallucination entirely on G2b's support head.

Store contract verified bitwise over 3200 `commit → read` pairs: shape 3200/3200,
`max|diff|` **0.00e+00**, detached 3200/3200, address↔content binding 3200/3200.

**The 3-token stress stratum is doubly OOD** — a 3-token query moves the writer's value span from
index 5 to 6 *and* uses query identities G2b never trained on. Only the formation column may be
attributed to the writer (95.7%, it nearly holds); the full-chain collapse is dominated by
retrieval at 7.9% and stays confounded.

**Two claims, neither cancelling the other**: `in-distribution 2-token closed-world component
closure` = **PASS**; `3-token span-shift robustness` = **FAIL**. Not open-set, not a
persistent-learning loop, not general pool capacity.

**Next — G3c**, deliberately excluded here: **dangling address** (index has the entry, content is
missing — partial write, GC race). Pin the contract first — `address visible ⇔ committed content
readable`, atomic commit, controller **fails closed** to abstain on a dangling read — then break
it on purpose and measure detection, repair, and zero hallucination. This is a storage/index
consistency problem; do not ask a learned support head to infer existence from similarity. Also
deferred: overwrite/reconsolidation, stale snapshots, write-address formation.

**G1 is sealed.** The default interface for the next stage is `zdelta`:
`K' = K_native + f_slot(z)` at the value token positions, all 16 (loop, layer) cache slots,
1.13M parameters, core frozen, 99.0% — `f(z)` is precomputable after retrieval so only the
native scaffold forward and the addition are online. `contextual` at 100% is kept as an upper
bound; the extra point is not worth making the synthesiser read native K/V online. Outstanding
debt: single seed.

**Next axis is retrieve, with the store and write frozen** — train only query/address/selector
and report ordered exact retrieval, per-step recall, hit/miss with distractors,
`executor | retrieval correct`, and end-to-end separately. Supervise hit/miss directly; §4.12
and §4.20 established that binding does not emerge from answer loss alone. Write and
consolidation open only after retrieve passes, never jointly at the start.

Everything is under oracle selection with a frozen store at 29M on the S₅ task, k≤4, one seed.
**Retrieval and write are not implemented.**

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
- **`--throttle` duty-cycles the GPU via sleeps.** The user now keeps a **100 W hardware power
  cap** set (they applied it themselves; `nvidia-smi -pl` needs sudo, which this agent does not
  have). The cap alone still lets the room accumulate heat over long queues, so **keep a modest
  software throttle on top of it — `--throttle 0.8` is the standing default** (~1.25× wall clock).
  Use lower values only if temperatures climb. Do not run long queues at `--throttle 1.0`.

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
- **The same trap breaks *measurement*, not just data design.** Comparing how distinguishable
  symbols are must use each symbol's span **as it appears in context**: `'f0'→[105,51]` but
  `' f0'→[341,51]`. Measuring the bare form reported 28 pairs of "identical" hidden states and
  produced a wrong claim ("the frozen core never saw these symbols so it does not encode them");
  the corrected span gave 7 pairs, none of them exact — the real issue was conditioning
  (σ_min/σ_max 1.65e-4), not absence. Any symbol-identity comparison must go through one
  canonical-span helper shared by every side — `g1_renderer.canonical_key_ids` /
  `locate_key_span`. Never re-derive the tokenization inline.
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
