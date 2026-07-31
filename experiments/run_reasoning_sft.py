"""從同一個 vanilla pretrain checkpoint 出發，切換架構旗標做 reasoning SFT。

實驗設計（比「四個架構各自 pretrain」乾淨）：
  * 只 pretrain 一次 → 所有變體從**位元相同**的權重出發，消除 init 變異
  * dense attention 不增加任何參數，state dict 可以完美載入（已驗證）
  * 因此 vanilla vs dense 的差異純粹來自「注意力連通方式」

注意 engram 的限制：它會多出 105M 隨機初始化的參數，只能在 SFT 階段學。
這對應「外掛式記憶」的概念，但資料量遠少於 pretrain，結果會偏悲觀 ——
那是掛載方式的限制，不是機制本身無效。
latent attention 的 o_proj 形狀不同，無法從 vanilla checkpoint 載入。

輸出 results_sft.json：訓練曲線 + held-out CoT loss（只算 response 段）。

用法：
    python experiments/run_reasoning_sft.py                  # 跑全部
    python experiments/run_reasoning_sft.py --only dense     # 只跑一個
"""
import os
import sys
import json
import time
import math
import argparse

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import torch

from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "reasoning_tokens.pt")
BASE_CKPT = os.path.join(HERE, "ckpt_vanilla.pth")
RESULTS = os.path.join(HERE, "results_sft.json")

BACKBONE = dict(
    hidden_size=512, num_hidden_layers=8, num_attention_heads=8,
    num_key_value_heads=2, vocab_size=6400, max_position_embeddings=1024,
)

CONFIGS = {
    "vanilla":   dict(use_engram=False, use_dense_attention=False),
    "dense":     dict(use_engram=False, use_dense_attention=True),
    "engram":    dict(use_engram=True,  use_dense_attention=False),
    "engram+ca": dict(use_engram=True,  use_dense_attention=True),
}
ENGRAM = dict(engram_offload_cpu=False, engram_layers=[2, 4, 6])

SEED = 42
BATCH_SIZE = 16
EPOCHS = 2
LR = 2e-5                # SFT 用比 pretrain 小得多的 LR
WARMUP_FRAC = 0.03
MIN_LR_RATIO = 0.1
WEIGHT_DECAY = 0.1
GRAD_CLIP = 1.0
EVAL_EVERY = 500
LOG_EVERY = 100
DEVICE = "cuda"


def make_labels(ids, plen, tlen, pad_id):
    """prompt 段與 padding 段都設 -100，只有 response 會算 loss。"""
    labels = ids.clone()
    ar = torch.arange(ids.shape[1], device=ids.device).unsqueeze(0)
    mask = (ar < plen.unsqueeze(1)) | (ar >= tlen.unsqueeze(1))
    labels[mask] = -100
    return labels


@torch.no_grad()
def evaluate(model, data, pad_id, max_batches=125):
    model.eval()
    ids_all, plen_all, tlen_all = data["val_ids"], data["val_plen"], data["val_tlen"]
    tot_loss, tot_tok = 0.0, 0
    for i in range(0, min(max_batches * BATCH_SIZE, ids_all.shape[0]), BATCH_SIZE):
        ids = ids_all[i:i + BATCH_SIZE].to(DEVICE).long()
        plen = plen_all[i:i + BATCH_SIZE].to(DEVICE).long()
        tlen = tlen_all[i:i + BATCH_SIZE].to(DEVICE).long()
        labels = make_labels(ids, plen, tlen, pad_id)
        with torch.amp.autocast(DEVICE, dtype=torch.bfloat16):
            logits = model(ids).logits
        # 用 token 加權平均，避免不同長度的樣本被等權對待
        x = logits[:, :-1].float().reshape(-1, logits.shape[-1])
        y = labels[:, 1:].reshape(-1)
        n = (y != -100).sum().item()
        loss = torch.nn.functional.cross_entropy(x, y, ignore_index=-100, reduction="sum")
        tot_loss += loss.item()
        tot_tok += n
    model.train()
    return tot_loss / max(tot_tok, 1)


def build(name):
    kw = dict(CONFIGS[name])
    if kw.get("use_engram"):
        kw.update(ENGRAM)
    torch.manual_seed(SEED)
    torch.cuda.manual_seed_all(SEED)
    model = MiniMindForCausalLM(MiniMindConfig(**BACKBONE, **kw))

    sd = torch.load(BASE_CKPT, map_location="cpu")
    missing, unexpected = model.load_state_dict(sd, strict=False)
    missing = [k for k in missing
               if not k.endswith(("freqs_cos", "freqs_sin", "multipliers", "offsets"))]
    n_new = sum(p.numel() for k, p in model.named_parameters() if k in missing)
    if missing:
        print(f"  ⚠️  {len(missing)} 個參數不在 checkpoint 中 → 隨機初始化 "
              f"({n_new/1e6:.2f}M)", flush=True)
    else:
        print(f"  ✅ checkpoint 完美轉移（零隨機初始化參數）", flush=True)
    if unexpected:
        raise RuntimeError(f"checkpoint 有多餘參數，架構對不上：{unexpected[:3]}")
    return model.to(DEVICE)


def run(name, data):
    print(f"\n{'='*66}\n  {name}\n{'='*66}", flush=True)
    torch.cuda.empty_cache(); torch.cuda.reset_peak_memory_stats()

    model = build(name)
    pad_id = data["pad_token_id"]
    train_ids, train_plen, train_tlen = data["train_ids"], data["train_plen"], data["train_tlen"]
    n_train = train_ids.shape[0]
    steps = (n_train // BATCH_SIZE) * EPOCHS
    warmup = int(steps * WARMUP_FRAC)
    print(f"  {steps} 步 ({EPOCHS} epochs × {n_train} 樣本)", flush=True)

    decay, no_decay = [], []
    for n, p in model.named_parameters():
        (no_decay if (p.ndim < 2 or "engram" in n or "embed_tokens" in n or "lm_head" in n)
         else decay).append(p)
    opt = torch.optim.AdamW(
        [{"params": decay, "weight_decay": WEIGHT_DECAY},
         {"params": no_decay, "weight_decay": 0.0}],
        lr=LR, betas=(0.9, 0.95), fused=True)

    g = torch.Generator().manual_seed(SEED)
    order = torch.cat([torch.randperm(n_train, generator=g) for _ in range(EPOCHS)])

    hist = {"step": [], "train_loss": []}
    val_hist = {"step": [], "val_loss": []}
    base_val = evaluate(model, data, pad_id)
    val_hist["step"].append(0); val_hist["val_loss"].append(base_val)
    print(f"  step     0  >>> val CoT loss={base_val:.4f} ppl={math.exp(min(base_val,20)):.2f} (SFT 前)", flush=True)

    model.train()
    t0 = time.time()
    running = torch.zeros((), device=DEVICE); running_n = 0

    for step in range(1, steps + 1):
        lr = LR * step / max(warmup, 1) if step < warmup else \
             LR * (MIN_LR_RATIO + (1 - MIN_LR_RATIO) * 0.5 *
                   (1 + math.cos(math.pi * (step - warmup) / max(steps - warmup, 1))))
        for pg in opt.param_groups:
            pg["lr"] = lr

        sel = order[(step - 1) * BATCH_SIZE:step * BATCH_SIZE]
        ids = train_ids[sel].to(DEVICE).long()
        plen = train_plen[sel].to(DEVICE).long()
        tlen = train_tlen[sel].to(DEVICE).long()
        labels = make_labels(ids, plen, tlen, pad_id)

        with torch.amp.autocast(DEVICE, dtype=torch.bfloat16):
            out = model(ids, labels=labels)
            loss = out.loss + out.aux_loss

        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), GRAD_CLIP)
        opt.step(); opt.zero_grad(set_to_none=True)
        running += out.loss.detach(); running_n += 1

        if step % LOG_EVERY == 0:
            avg = (running / running_n).item()
            hist["step"].append(step); hist["train_loss"].append(avg)
            running = torch.zeros((), device=DEVICE); running_n = 0
            if step % (LOG_EVERY * 10) == 0:
                el = time.time() - t0
                print(f"  step {step:5d}/{steps}  train={avg:.4f}  lr={lr:.2e}  "
                      f"{step/el:.2f} it/s  eta {el/step*(steps-step)/60:.1f}min", flush=True)

        if step % EVAL_EVERY == 0 or step == steps:
            vl = evaluate(model, data, pad_id)
            val_hist["step"].append(step); val_hist["val_loss"].append(vl)
            print(f"  step {step:5d}  >>> val CoT loss={vl:.4f} ppl={math.exp(min(vl,20)):.2f}", flush=True)

    ckpt = os.path.join(HERE, f"sft_{name.replace('+','_')}.pth")
    torch.save({k: v.cpu() for k, v in model.state_dict().items()}, ckpt)

    elapsed = time.time() - t0
    result = {
        "name": name, "flags": CONFIGS[name],
        "params_total_M": sum(p.numel() for p in model.parameters()) / 1e6,
        "train_hist": hist, "val_hist": val_hist,
        "val_loss_before_sft": base_val,
        "final_val_loss": val_hist["val_loss"][-1],
        "best_val_loss": min(val_hist["val_loss"]),
        "final_val_ppl": math.exp(min(val_hist["val_loss"][-1], 20)),
        "wall_clock_s": elapsed, "it_per_s": steps / elapsed,
        "peak_vram_GiB": torch.cuda.max_memory_allocated() / 2**30,
        "checkpoint": ckpt,
    }
    print(f"  完成：{elapsed/60:.1f} min  val CoT loss {base_val:.4f} → {result['final_val_loss']:.4f}", flush=True)
    del model, opt; torch.cuda.empty_cache()
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None)
    ap.add_argument("--epochs", type=int, default=None)
    args = ap.parse_args()
    global EPOCHS
    if args.epochs:
        EPOCHS = args.epochs

    if not os.path.exists(BASE_CKPT):
        raise SystemExit(f"找不到 pretrain checkpoint：{BASE_CKPT}\n"
                         f"請先跑：python experiments/run_ablation.py --only vanilla")

    data = torch.load(DATA)
    print(f"資料：train={tuple(data['train_ids'].shape)} val={tuple(data['val_ids'].shape)}")
    print(f"基底 checkpoint：{BASE_CKPT}")

    results = json.load(open(RESULTS)) if os.path.exists(RESULTS) else {}
    for name in ([args.only] if args.only else list(CONFIGS)):
        results[name] = run(name, data)
        json.dump(results, open(RESULTS, "w"), indent=2, ensure_ascii=False)

    print(f"\n{'='*66}\n  總結（val CoT loss，越低越好）\n{'='*66}")
    print(f"{'config':12s} {'params':>9s} {'SFT前':>8s} {'SFT後':>8s} {'改善':>8s} {'ppl':>8s} {'min':>6s}")
    for n in CONFIGS:
        if n in results:
            r = results[n]
            print(f"{n:12s} {r['params_total_M']:8.1f}M {r['val_loss_before_sft']:8.4f} "
                  f"{r['final_val_loss']:8.4f} {r['final_val_loss']-r['val_loss_before_sft']:+8.4f} "
                  f"{r['final_val_ppl']:8.2f} {r['wall_clock_s']/60:5.1f}")


if __name__ == "__main__":
    main()
