"""量化的推理評測：held-out CoT loss + 生成式 exact-match 正確率。

為什麼要另外寫一支：倉庫裡的 `eval_reasoning.py` 只是把 4 個寫死的問題丟給模型、
串流輸出讓人用眼睛看，沒有測試集也沒有分數。要比較架構就必須有可重複的數字。

兩個指標的定位不同，都要看：

  1. **CoT loss**（連續、高解析度）
     只計算 response 段的 token loss。就算正確率是 0，它仍能分出架構高下。
     這是 29M 這種規模下唯一有解析度的指標。

  2. **exact-match 正確率**（離散、可解釋）
     真的讓模型生成，抽出最後一個數字跟標準答案比對。
     29M 模型預期會非常低（可能 0%），所以附上 Wilson 95% 信賴區間 ——
     兩個都是 0/500 跟兩個都是 3/500，在統計上是分不開的。

用法：
    python experiments/eval_reasoning_quant.py --ckpt experiments/sft_dense.pth --dense 1
    python experiments/eval_reasoning_quant.py --all      # 掃過所有 sft_*.pth
"""
import os
import re
import sys
import json
import math
import glob
import argparse

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import torch
from transformers import AutoTokenizer

from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
BACKBONE = dict(
    hidden_size=512, num_hidden_layers=8, num_attention_heads=8,
    num_key_value_heads=2, vocab_size=6400, max_position_embeddings=1024,
)
DEVICE = "cuda"

# checkpoint 檔名 → 架構旗標
FLAGS = {
    "vanilla":   dict(use_engram=False, use_dense_attention=False),
    "dense":     dict(use_engram=False, use_dense_attention=True),
    "engram":    dict(use_engram=True,  use_dense_attention=False),
    "engram_ca": dict(use_engram=True,  use_dense_attention=True),
}
NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def wilson(k, n, z=1.96):
    """Wilson 95% 信賴區間。低正確率 + 小樣本時，normal approximation 會失真。"""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def final_number(text):
    """抽出文字裡最後一個數字，當作模型的答案。"""
    m = NUM_RE.findall(text.replace(",", ""))
    if not m:
        return None
    try:
        return round(float(m[-1].replace(",", "")), 4)
    except ValueError:
        return None


def load_model(ckpt, flags):
    kw = dict(flags)
    if kw.get("use_engram"):
        kw.update(engram_offload_cpu=False, engram_layers=[2, 4, 6])
    model = MiniMindForCausalLM(MiniMindConfig(**BACKBONE, **kw))
    sd = torch.load(ckpt, map_location="cpu")
    missing, unexpected = model.load_state_dict(sd, strict=False)
    missing = [k for k in missing
               if not k.endswith(("freqs_cos", "freqs_sin", "multipliers", "offsets"))]
    if missing or unexpected:
        raise RuntimeError(f"架構與 checkpoint 不符：缺 {missing[:3]} 多 {unexpected[:3]}")
    return model.to(DEVICE).eval()


@torch.no_grad()
def cot_loss(model, data, batch=16, max_batches=125):
    """只在 response 段計算的 token 加權平均 loss。"""
    ids_all, plen_all, tlen_all = data["val_ids"], data["val_plen"], data["val_tlen"]
    tot, ntok = 0.0, 0
    for i in range(0, min(max_batches * batch, ids_all.shape[0]), batch):
        ids = ids_all[i:i + batch].to(DEVICE).long()
        plen = plen_all[i:i + batch].to(DEVICE).long()
        tlen = tlen_all[i:i + batch].to(DEVICE).long()
        labels = ids.clone()
        ar = torch.arange(ids.shape[1], device=DEVICE).unsqueeze(0)
        labels[(ar < plen.unsqueeze(1)) | (ar >= tlen.unsqueeze(1))] = -100
        with torch.amp.autocast(DEVICE, dtype=torch.bfloat16):
            logits = model(ids).logits
        x = logits[:, :-1].float().reshape(-1, logits.shape[-1])
        y = labels[:, 1:].reshape(-1)
        tot += torch.nn.functional.cross_entropy(x, y, ignore_index=-100, reduction="sum").item()
        ntok += (y != -100).sum().item()
    return tot / max(ntok, 1)


@torch.no_grad()
def exact_match(model, tok, raw, n=500, max_new_tokens=320):
    """真的生成，抽最後一個數字比對。"""
    hit, seen, samples = 0, 0, []
    for item in raw[:n]:
        gold = final_number(item["response"])
        if gold is None:
            continue
        ids = tok(tok.bos_token + item["prompt"], add_special_tokens=False,
                  return_tensors="pt").input_ids.to(DEVICE)
        out = model.generate(ids, max_new_tokens=max_new_tokens, do_sample=False,
                             eos_token_id=tok.eos_token_id)
        gen = tok.decode(out[0][ids.shape[1]:], skip_special_tokens=True)
        pred = final_number(gen)
        seen += 1
        ok = pred is not None and abs(pred - gold) < 1e-4
        hit += ok
        if len(samples) < 3:
            samples.append({"prompt": item["prompt"][:60], "gold": gold,
                            "pred": pred, "gen": gen[:120]})
    lo, hi = wilson(hit, seen)
    return {"correct": hit, "total": seen, "acc": hit / max(seen, 1),
            "ci95": [lo, hi], "samples": samples}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt")
    ap.add_argument("--arch", help="vanilla / dense / engram / engram_ca")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--gen-n", type=int, default=500, help="做 exact-match 的樣本數")
    ap.add_argument("--skip-gen", action="store_true", help="只算 CoT loss（快很多）")
    ap.add_argument("--out", default=os.path.join(HERE, "results_eval.json"))
    args = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    data = torch.load(os.path.join(HERE, "reasoning_tokens.pt"))
    raw = json.load(open(os.path.join(HERE, "reasoning_val_raw.json"), encoding="utf-8"))

    targets = []
    if args.all:
        for p in sorted(glob.glob(os.path.join(HERE, "sft_*.pth"))):
            targets.append((os.path.basename(p)[4:-4], p))
    else:
        targets.append((args.arch, args.ckpt))

    results = json.load(open(args.out)) if os.path.exists(args.out) else {}
    for arch, ckpt in targets:
        if arch not in FLAGS:
            print(f"⚠️  跳過 {ckpt}：未知架構 '{arch}'"); continue
        print(f"\n=== {arch} ({os.path.basename(ckpt)}) ===", flush=True)
        model = load_model(ckpt, FLAGS[arch])
        r = {"arch": arch, "ckpt": ckpt}
        r["cot_loss"] = cot_loss(model, data)
        r["cot_ppl"] = math.exp(min(r["cot_loss"], 20))
        print(f"  CoT loss = {r['cot_loss']:.4f}  ppl = {r['cot_ppl']:.2f}", flush=True)
        if not args.skip_gen:
            em = exact_match(model, tok, raw, n=args.gen_n)
            r["exact_match"] = em
            print(f"  exact-match = {em['correct']}/{em['total']} = {em['acc']:.2%}  "
                  f"(95% CI {em['ci95'][0]:.2%}–{em['ci95'][1]:.2%})", flush=True)
        results[arch] = r
        json.dump(results, open(args.out, "w"), indent=2, ensure_ascii=False)
        del model; torch.cuda.empty_cache()

    print(f"\n{'='*72}\n  總結\n{'='*72}")
    print(f"{'arch':12s} {'CoT loss':>10s} {'CoT ppl':>9s} {'exact-match':>13s} {'95% CI':>18s}")
    for a in FLAGS:
        if a in results:
            r = results[a]
            em = r.get("exact_match")
            es = f"{em['correct']}/{em['total']} = {em['acc']:.1%}" if em else "—"
            ci = f"{em['ci95'][0]:.1%}–{em['ci95'][1]:.1%}" if em else "—"
            print(f"{a:12s} {r['cot_loss']:10.4f} {r['cot_ppl']:9.2f} {es:>13s} {ci:>18s}")
    print("\n註：若各架構的 95% CI 重疊，就不能宣稱正確率有差異。")


if __name__ == "__main__":
    main()
