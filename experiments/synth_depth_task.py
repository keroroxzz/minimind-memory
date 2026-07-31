"""深度可控的合成推理任務 —— 直接測「單次前向傳播能做幾步組合推理」。

為什麼需要這個
--------------
1. 通用推理基準（GSM8K 等）在 29M 規模下沒有解析度，四個架構大概率全 0%。
2. Belle 的 exact-match 不可靠：中文數學解答常以餘數或多部分答案結尾，
   「取最後一個數字」四題只對一題。等於在量雜訊。
3. 這個任務的答案由建構方式決定，完全無歧義。

核心設計：**目標只有最終答案，不含任何中間步驟**
----------------------------------------------
CA / looped transformer 買到的是「單次前向傳播內的深度」。
若允許模型輸出 CoT，它可以一個 token 做一步，深度就從「層」轉移到
「自迴歸步數」，架構差異會被完全掩蓋。因此目標序列只有答案本身。

任務格式（k 步依賴鏈，每步都依賴上一步的結果）：
    a=53 b=a+7 c=b*3 d=c-9 求d=          → 81
值域取 mod 100，答案固定 100 類 → 隨機基準 1%。

用法：
    python experiments/synth_depth_task.py --gen             # 產生資料
    python experiments/synth_depth_task.py --run vanilla     # 訓練 + 評測
    python experiments/synth_depth_task.py --run dense
    python experiments/synth_depth_task.py --report
"""
import os
import sys
import json
import time
import math
import random
import argparse

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import torch
from transformers import AutoTokenizer

from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "synth_depth.pt")
RESULTS = os.path.join(HERE, "results_synth.json")
BASE_CKPT = os.path.join(HERE, "ckpt_vanilla.pth")

BACKBONE = dict(hidden_size=512, num_hidden_layers=8, num_attention_heads=8,
                num_key_value_heads=2, vocab_size=6400, max_position_embeddings=1024)
CONFIGS = {
    "vanilla":   dict(use_engram=False, use_dense_attention=False),
    "dense":     dict(use_engram=False, use_dense_attention=True),
    "engram":    dict(use_engram=True,  use_dense_attention=False),
    "engram+ca": dict(use_engram=True,  use_dense_attention=True),
}
ENGRAM = dict(engram_offload_cpu=False, engram_layers=[2, 4, 6])

MAX_K = 6
NAMES = "abcdefghij"
SEQ_LEN = 96
DEVICE = "cuda"
SEED = 42


# ----------------------------------------------------------------- 資料產生
def make_one(k, rng, v0_lo=10, v0_hi=79):
    """產生一條 k 步依賴鏈。每一步都必須用到前一步的結果。

    train / val 用**互斥的起始值域**來保證不重疊，而不是靠拒絕採樣去找唯一解 ——
    k=1 時整個問題空間只有 90×3×8=2160 種，要求兩萬條唯一會讓迴圈永遠跑不完。
    這樣同時也更嚴格：val 的起始值在訓練時從未出現過，測的是泛化而非記憶。
    """
    v = rng.randint(v0_lo, v0_hi)
    parts = [f"{NAMES[0]}={v}"]
    for i in range(1, k + 1):
        op = rng.choice("+-*")
        rhs = rng.randint(2, 9)
        v = {"+": v + rhs, "-": v - rhs, "*": v * rhs}[op] % 100
        parts.append(f"{NAMES[i]}={NAMES[i-1]}{op}{rhs}")
    prompt = " ".join(parts) + f" 求{NAMES[k]}="
    return prompt, str(v)


def gen(args):
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    rng = random.Random(SEED)
    per_k_train = args.train_per_k
    per_k_val = args.val_per_k

    def build(n_per_k, rng, v0_lo, v0_hi, unique):
        """unique=True 時去重（val 用）；train 允許重複，反正就是重複樣本。"""
        rows = []
        for k in range(1, MAX_K + 1):
            seen, made, attempts = set(), 0, 0
            while made < n_per_k and attempts < n_per_k * 50:
                attempts += 1
                p, a = make_one(k, rng, v0_lo, v0_hi)
                if unique:
                    if p in seen:
                        continue
                    seen.add(p)
                rows.append({"k": k, "prompt": p, "answer": a})
                made += 1
            if made < n_per_k:
                print(f"  ⚠️  k={k} 只產生 {made}/{n_per_k} 條（問題空間已窮盡）")
        return rows

    # 起始值域互斥 → train 與 val 保證零重疊，且 val 測的是對未見起始值的泛化
    train_rows = build(per_k_train, rng, 10, 79, unique=False)
    val_rows = build(per_k_val, random.Random(SEED + 999), 80, 99, unique=True)
    overlap = {r["prompt"] for r in train_rows} & {r["prompt"] for r in val_rows}
    assert not overlap, f"train/val 重疊 {len(overlap)} 條"

    def encode(rows):
        ids, plen, tlen, ks = [], [], [], []
        for r in rows:
            p = tok(tok.bos_token + r["prompt"], add_special_tokens=False)["input_ids"]
            a = tok(r["answer"] + tok.eos_token, add_special_tokens=False)["input_ids"]
            t = len(p) + len(a)
            if t > SEQ_LEN:
                continue
            ids.append(p + a + [tok.pad_token_id] * (SEQ_LEN - t))
            plen.append(len(p)); tlen.append(t); ks.append(r["k"])
        return (torch.tensor(ids, dtype=torch.int16), torch.tensor(plen, dtype=torch.int16),
                torch.tensor(tlen, dtype=torch.int16), torch.tensor(ks, dtype=torch.int8))

    tr = encode(train_rows); va = encode(val_rows)
    torch.save({"train": tr, "val": va, "val_rows": val_rows,
                "seq_len": SEQ_LEN, "pad_token_id": tok.pad_token_id}, DATA)
    print(f"✅ {DATA}")
    print(f"   train {tr[0].shape[0]} 條 / val {va[0].shape[0]} 條，k=1..{MAX_K}")
    print(f"   隨機基準 = 1.0%（答案 100 類）")
    print(f"   範例 k=3: {[r for r in train_rows if r['k']==3][0]}")


# ----------------------------------------------------------------- 訓練 / 評測
def labels_of(ids, plen, tlen):
    lab = ids.clone()
    ar = torch.arange(ids.shape[1], device=ids.device).unsqueeze(0)
    lab[(ar < plen.unsqueeze(1)) | (ar >= tlen.unsqueeze(1))] = -100
    return lab


@torch.no_grad()
def acc_by_k(model, tok, val_rows, max_per_k=200):
    """逐 k 計算正確率。貪婪解碼，答案必須完全相符。"""
    from collections import defaultdict
    hit, tot = defaultdict(int), defaultdict(int)
    by_k = defaultdict(list)
    for r in val_rows:
        by_k[r["k"]].append(r)
    for k in sorted(by_k):
        for r in by_k[k][:max_per_k]:
            ids = tok(tok.bos_token + r["prompt"], add_special_tokens=False,
                      return_tensors="pt").input_ids.to(DEVICE)
            out = model.generate(ids, max_new_tokens=6, do_sample=False,
                                 eos_token_id=tok.eos_token_id)
            gen_txt = tok.decode(out[0][ids.shape[1]:], skip_special_tokens=True).strip()
            tot[k] += 1
            hit[k] += (gen_txt == r["answer"])
    return {k: {"correct": hit[k], "total": tot[k], "acc": hit[k] / max(tot[k], 1)}
            for k in sorted(tot)}


def run(name, args):
    d = torch.load(DATA)
    (tr_ids, tr_plen, tr_tlen, _), (va_ids, va_plen, va_tlen, _) = d["train"], d["val"]
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))

    kw = dict(CONFIGS[name])
    if kw.get("use_engram"):
        kw.update(ENGRAM)
    torch.manual_seed(SEED); torch.cuda.manual_seed_all(SEED)
    model = MiniMindForCausalLM(MiniMindConfig(**BACKBONE, **kw))
    if args.from_pretrain and os.path.exists(BASE_CKPT):
        sd = torch.load(BASE_CKPT, map_location="cpu")
        miss, unexp = model.load_state_dict(sd, strict=False)
        if unexp:
            raise RuntimeError(f"checkpoint 與架構不符：{unexp[:3]}")
        print(f"  從 pretrain checkpoint 出發", flush=True)
    model = model.to(DEVICE)

    torch.cuda.empty_cache(); torch.cuda.reset_peak_memory_stats()
    bs, steps = args.batch_size, args.steps
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=0.01,
                            betas=(0.9, 0.95), fused=True)
    g = torch.Generator().manual_seed(SEED)
    n = tr_ids.shape[0]
    order = torch.cat([torch.randperm(n, generator=g) for _ in range(steps * bs // n + 2)])

    print(f"\n{'='*60}\n  {name} — {steps} 步\n{'='*60}", flush=True)
    model.train(); t0 = time.time()
    run_loss = torch.zeros((), device=DEVICE); run_n = 0
    for step in range(1, steps + 1):
        lr = args.lr * min(1.0, step / 200) * (0.1 + 0.9 * 0.5 *
             (1 + math.cos(math.pi * min(1.0, step / steps))))
        for pg in opt.param_groups:
            pg["lr"] = lr
        sel = order[(step - 1) * bs:step * bs]
        ids = tr_ids[sel].to(DEVICE).long()
        lab = labels_of(ids, tr_plen[sel].to(DEVICE).long(), tr_tlen[sel].to(DEVICE).long())
        with torch.amp.autocast(DEVICE, dtype=torch.bfloat16):
            out = model(ids, labels=lab)
            loss = out.loss + out.aux_loss
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        opt.step(); opt.zero_grad(set_to_none=True)
        run_loss += out.loss.detach(); run_n += 1
        if step % 500 == 0:
            print(f"  step {step:5d}/{steps} loss={(run_loss/run_n).item():.4f} "
                  f"{step/(time.time()-t0):.1f} it/s", flush=True)
            run_loss = torch.zeros((), device=DEVICE); run_n = 0

    model.eval()
    per_k = acc_by_k(model, tok, d["val_rows"], max_per_k=args.eval_per_k)
    elapsed = time.time() - t0
    overall = sum(v["correct"] for v in per_k.values()) / max(sum(v["total"] for v in per_k.values()), 1)
    print(f"\n  逐深度正確率（隨機基準 1.0%）：")
    for k, v in per_k.items():
        print(f"    k={k}  {v['correct']:3d}/{v['total']:3d} = {v['acc']:6.1%}")
    print(f"  整體 {overall:.1%}   {elapsed/60:.1f} min", flush=True)

    res = json.load(open(RESULTS)) if os.path.exists(RESULTS) else {}
    res[name] = {"per_k": {str(k): v for k, v in per_k.items()}, "overall": overall,
                 "params_M": sum(p.numel() for p in model.parameters()) / 1e6,
                 "wall_clock_s": elapsed, "steps": steps,
                 "peak_vram_GiB": torch.cuda.max_memory_allocated() / 2**30}
    json.dump(res, open(RESULTS, "w"), indent=2, ensure_ascii=False)
    del model, opt; torch.cuda.empty_cache()


def report():
    res = json.load(open(RESULTS))
    ks = sorted({int(k) for r in res.values() for k in r["per_k"]})
    print(f"\n{'config':12s} " + " ".join(f"{'k='+str(k):>8s}" for k in ks) + f" {'整體':>8s}")
    print("-" * (13 + 9 * len(ks) + 9))
    for n in CONFIGS:
        if n in res:
            r = res[n]
            row = " ".join(f"{r['per_k'].get(str(k),{'acc':float('nan')})['acc']:7.1%} " for k in ks)
            print(f"{n:12s} {row}{r['overall']:7.1%}")
    print("\n隨機基準 1.0%。若某架構的曲線隨 k 衰減得較慢，即為單次前向推理深度的直接證據。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--gen", action="store_true")
    ap.add_argument("--run", default=None)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--train-per-k", type=int, default=20000)
    ap.add_argument("--val-per-k", type=int, default=300)
    ap.add_argument("--steps", type=int, default=6000)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--eval-per-k", type=int, default=200)
    ap.add_argument("--from-pretrain", type=int, default=1)
    a = ap.parse_args()
    if a.gen: gen(a)
    elif a.run: run(a.run, a)
    elif a.report: report()
    else: ap.print_help()
