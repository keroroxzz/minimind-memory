"""vanilla / engram / complete-attention / engram+complete-attention 對照實驗。

設計原則 —— 除了架構旗標之外，其他一切都必須相同：
  * 同一份預先 tokenize 的資料 (data_tokens.pt)，同樣的批次順序
  * 同樣的 seed、optimizer、LR schedule、grad clip、步數
  * 同樣的 eval 切分 (訓練期間完全沒看過的 2000 條序列)

輸出 results.json，內容包含每個 config 的訓練/驗證 loss 曲線與最終指標。

用法：
    python experiments/run_ablation.py                # 跑全部四個
    python experiments/run_ablation.py --only vanilla # 只跑其中一個
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
DATA = os.path.join(HERE, "data_tokens.pt")
RESULTS = os.path.join(HERE, "results.json")


# 骨幹設定：四個 config 完全共用
BACKBONE = dict(
    hidden_size=512,
    num_hidden_layers=8,
    num_attention_heads=8,
    num_key_value_heads=2,
    vocab_size=6400,
    max_position_embeddings=1024,
)

# 只有這裡不同。
# 注意：MiniMindConfig 的 use_engram 預設是 True，所以每個旗標都必須寫死，
# 否則 "vanilla" 會偷偷帶著 engram 跑 (實測會變成 134M 參數而非 29M)。
CONFIGS = {
    "vanilla":      dict(use_engram=False, use_dense_attention=False),
    "engram":       dict(use_engram=True,  use_dense_attention=False),
    "complete_att": dict(use_engram=False, use_dense_attention=True),
    "engram+ca":    dict(use_engram=True,  use_dense_attention=True),
}

# Engram 共用設定。offload_cpu=False：把表放 GPU。
# 實測 CPU offload 慢 6.2 倍 (1.30 vs 8.03 step/s)，而 12G VRAM 放得下，
# 且這只影響「表存在哪」，不影響任何數學運算。
ENGRAM = dict(engram_offload_cpu=False, engram_layers=[2, 4, 6])

SEED = 42
BATCH_SIZE = 16
STEPS = 30000          # 30000 x 16 x 512 = 245.8M tokens = 剛好一個 epoch
WARMUP = 1500          # 5% of STEPS
LR = 5e-4
MIN_LR_RATIO = 0.1
GRAD_CLIP = 1.0
WEIGHT_DECAY = 0.1
VERBOSE_GROUPS = True
EVAL_EVERY = 1000
EVAL_BATCHES = 125   # 2000 條 val 全用上
LOG_EVERY = 200
DEVICE = "cuda"


def lr_at(step):
    if step < WARMUP:
        return LR * step / max(WARMUP, 1)
    progress = (step - WARMUP) / max(STEPS - WARMUP, 1)
    cos = 0.5 * (1.0 + math.cos(math.pi * progress))
    return LR * (MIN_LR_RATIO + (1 - MIN_LR_RATIO) * cos)


@torch.no_grad()
def evaluate(model, val_ids):
    model.eval()
    total_loss, total_batches = 0.0, 0
    for i in range(0, min(EVAL_BATCHES * BATCH_SIZE, val_ids.shape[0]), BATCH_SIZE):
        ids = val_ids[i:i + BATCH_SIZE].to(DEVICE, non_blocking=True).long()
        lbl = ids
        with torch.amp.autocast(DEVICE, dtype=torch.bfloat16):
            out = model(ids, labels=lbl)
        # 只看語言模型 loss，不含 aux_loss —— 那是正則項，不是能力指標
        total_loss += out.loss.float().item()
        total_batches += 1
    model.train()
    return total_loss / max(total_batches, 1)


def build(name):
    kw = dict(CONFIGS[name])
    if kw.get("use_engram"):
        kw.update(ENGRAM)
    torch.manual_seed(SEED)
    torch.cuda.manual_seed_all(SEED)
    cfg = MiniMindConfig(**BACKBONE, **kw)
    return MiniMindForCausalLM(cfg).to(DEVICE)


def run(name, data, seq_len=512):
    print(f"\n{'='*66}\n  {name}\n{'='*66}", flush=True)
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()

    model = build(name)
    n_total = sum(p.numel() for p in model.parameters())
    n_engram = sum(p.numel() for n, p in model.named_parameters() if "engram" in n)
    print(f"參數量 total={n_total/1e6:.2f}M  engram={n_engram/1e6:.2f}M  backbone={(n_total-n_engram)/1e6:.2f}M", flush=True)

    # 參數分組：embedding / norm / engram 表不套用 weight decay。
    # 這對 engram 特別關鍵 —— AdamW 每一步會衰減「所有」參數，但 105M 列的雜湊表
    # 每步只有約 0.4% 的列拿得到梯度，統一衰減等於持續把沒被造訪的列往 0 拉，
    # 屬於對 engram 不公平的懲罰。norm/embedding 不做 decay 也是通行做法。
    decay, no_decay = [], []
    for n, p in model.named_parameters():
        if not p.requires_grad:
            continue
        if p.ndim < 2 or "engram" in n or "embed_tokens" in n or "lm_head" in n:
            no_decay.append(p)
        else:
            decay.append(p)
    opt = torch.optim.AdamW(
        [{"params": decay, "weight_decay": WEIGHT_DECAY},
         {"params": no_decay, "weight_decay": 0.0}],
        lr=LR, betas=(0.9, 0.95), fused=True)
    if VERBOSE_GROUPS:
        print(f"  weight decay: {sum(p.numel() for p in decay)/1e6:.2f}M 套用 / "
              f"{sum(p.numel() for p in no_decay)/1e6:.2f}M 豁免", flush=True)
    train_ids, val_ids = data["train_ids"], data["val_ids"]

    # 固定批次順序：所有 config 看到完全相同的資料序列
    g = torch.Generator().manual_seed(SEED)
    order = torch.randperm(train_ids.shape[0], generator=g)

    hist = {"step": [], "train_loss": [], "lr": []}
    val_hist = {"step": [], "val_loss": []}
    model.train()
    t0 = time.time()
    # loss 累加在 GPU 上，只在 log 時才 .item()。每步 .item() 會強制
    # cudaStreamSynchronize，讓 CPU 無法提前排下一步的 kernel，形成 step 邊界氣泡。
    running = torch.zeros((), device=DEVICE)
    running_n = 0

    for step in range(1, STEPS + 1):
        lr = lr_at(step)
        for pg in opt.param_groups:
            pg["lr"] = lr

        sel = order[((step - 1) * BATCH_SIZE) % train_ids.shape[0]:][:BATCH_SIZE]
        if sel.shape[0] < BATCH_SIZE:  # 繞回開頭
            sel = order[:BATCH_SIZE]
        # 資料以 int16 儲存 (省 4 倍 RAM)，逐 batch 轉回 long
        ids = train_ids[sel].to(DEVICE, non_blocking=True).long()
        lbl = ids  # packing 之後沒有 padding，每個位置都是有效預測目標

        with torch.amp.autocast(DEVICE, dtype=torch.bfloat16):
            out = model(ids, labels=lbl)
            loss = out.loss + out.aux_loss

        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), GRAD_CLIP)
        opt.step()
        opt.zero_grad(set_to_none=True)

        running += out.loss.detach()
        running_n += 1

        if step % LOG_EVERY == 0:
            avg = (running / running_n).item()
            hist["step"].append(step)
            hist["train_loss"].append(avg)
            hist["lr"].append(lr)
            running = torch.zeros((), device=DEVICE)
            running_n = 0
            if step % (LOG_EVERY * 4) == 0:
                el = time.time() - t0
                eta = el / step * (STEPS - step)
                print(f"  step {step:5d}/{STEPS}  train={avg:.4f}  lr={lr:.2e}  "
                      f"{step/el:.2f} it/s  eta {eta/60:.1f}min", flush=True)

        if step % EVAL_EVERY == 0 or step == STEPS:
            vl = evaluate(model, val_ids)
            val_hist["step"].append(step)
            val_hist["val_loss"].append(vl)
            print(f"  step {step:5d}  >>> val_loss={vl:.4f}  ppl={math.exp(min(vl,20)):.2f}", flush=True)

    elapsed = time.time() - t0
    peak = torch.cuda.max_memory_allocated() / 2**30
    final_val = val_hist["val_loss"][-1]

    result = {
        "name": name,
        "flags": CONFIGS[name],
        "params_total_M": n_total / 1e6,
        "params_engram_M": n_engram / 1e6,
        "params_backbone_M": (n_total - n_engram) / 1e6,
        "train_hist": hist,
        "val_hist": val_hist,
        "final_val_loss": final_val,
        "final_val_ppl": math.exp(min(final_val, 20)),
        "best_val_loss": min(val_hist["val_loss"]),
        "wall_clock_s": elapsed,
        "it_per_s": STEPS / elapsed,
        "peak_vram_GiB": peak,
        "tokens_seen_M": STEPS * BATCH_SIZE * seq_len / 1e6,
    }
    print(f"  完成：{elapsed/60:.1f} min  peak={peak:.2f} GiB  final val_loss={final_val:.4f} "
          f"ppl={result['final_val_ppl']:.2f}", flush=True)

    del model, opt
    torch.cuda.empty_cache()
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="只跑指定的 config")
    ap.add_argument("--steps", type=int, default=None)
    ap.add_argument("--out", default=None, help="輸出檔名")
    args = ap.parse_args()

    global STEPS, RESULTS
    if args.steps:
        STEPS = args.steps
    if args.out:
        RESULTS = os.path.join(HERE, args.out)

    data = torch.load(DATA)
    print(f"資料：train={tuple(data['train_ids'].shape)} val={tuple(data['val_ids'].shape)} dtype={data['train_ids'].dtype}")
    print(f"設定：steps={STEPS} bs={BATCH_SIZE} seq={data['seq_len']} lr={LR} seed={SEED}")
    print(f"每個 config 看到 {STEPS*BATCH_SIZE*data['seq_len']/1e6:.2f}M tokens")

    results = {}
    if os.path.exists(RESULTS):
        results = json.load(open(RESULTS))

    names = [args.only] if args.only else list(CONFIGS)
    for name in names:
        results[name] = run(name, data, data["seq_len"])
        json.dump(results, open(RESULTS, "w"), indent=2, ensure_ascii=False)
        print(f"  → 已寫入 {RESULTS}", flush=True)

    print(f"\n{'='*66}\n  總結\n{'='*66}")
    print(f"{'config':14s} {'params':>10s} {'val_loss':>10s} {'ppl':>9s} {'min':>7s} {'VRAM':>7s}")
    for n in CONFIGS:
        if n in results:
            r = results[n]
            print(f"{n:14s} {r['params_total_M']:9.1f}M {r['final_val_loss']:10.4f} "
                  f"{r['final_val_ppl']:9.2f} {r['wall_clock_s']/60:6.1f} {r['peak_vram_GiB']:6.2f}G")


if __name__ == "__main__":
    main()
