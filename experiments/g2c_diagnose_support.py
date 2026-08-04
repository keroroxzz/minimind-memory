"""G2c 診斷：hit/miss 崩掉是 **threshold 轉移失敗** 還是 **score 本身不可分**？

兩者的修法完全不同，不能混為一談：
  - score 可分、threshold 放錯   → calibration 的覆蓋範圍問題
  - score 本身不可分              → support head 從未學會這個判別，是能力問題

所以在 train / cal / test 三個身分集合上各報兩個數字：
  1. **凍結 threshold**（由 cal 校出、之後不再動）下的 hit/miss —— 這是可宣稱的數字
  2. **oracle 最佳 threshold** 下的 hit/miss —— **上界診斷，不可作為結果宣稱**

⚠️ oracle threshold 是在該集合自己上掃出來的，用它報成績就是 calibration→evaluation
   重用（本專案已經犯過一次，§4.27 修掉）。這裡只用來回答「score 分不分得開」。
"""
import argparse
import glob
import json
import os
import random
import sys
from collections import defaultdict

import torch

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import g1_renderer as R
from g1_train import ARCH, BACKBONE, DEVICE
from g2_train import CORE_ZD, key_span_embeddings
from model.memory_module import ADDR_DIM, Retriever, TiedAddressEncoder
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))


def best_acc(scores, gold):
    """該集合上可達到的最高 hit/miss —— **上界診斷**，不是成績。"""
    lo, hi = float(scores.min()) - 1, float(scores.max()) + 1
    grid = torch.linspace(lo, hi, 801)
    return max(((scores > t) == gold).float().mean().item() for t in grid)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", default=None)
    ap.add_argument("--results", default="results_g2c_seed42.json")
    ap.add_argument("--val-per-k", type=int, default=100)
    ap.add_argument("--max-k", type=int, default=4)
    ap.add_argument("--p-missing", type=float, default=0.15)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    ck = a.ckpt or sorted(glob.glob(os.path.join(HERE, "g2c_retriever_*.pth")),
                          key=os.path.getmtime)[-1]
    res = json.load(open(os.path.join(HERE, a.results)))
    thr = res["threshold"]

    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    m = MiniMindForCausalLM(MiniMindConfig(**BACKBONE, **ARCH)).to(DEVICE).eval()
    m.load_state_dict(torch.load(os.path.join(HERE, CORE_ZD), map_location="cpu")["model"],
                      strict=False)
    blob = torch.load(ck, map_location="cpu")
    ret = Retriever(BACKBONE["hidden_size"], ADDR_DIM).to(DEVICE).eval()
    enc = TiedAddressEncoder(BACKBONE["hidden_size"], ADDR_DIM).to(DEVICE).eval()
    ret.load_state_dict(blob["retriever"]); enc.load_state_dict(blob["encoder"])
    sp_e, sp_m = key_span_embeddings(tok, m.model.embed_tokens, R.ALL_KEYS)
    print(f"  {os.path.basename(ck)}   凍結 threshold = {thr:+.3f}\n")

    tr = R.build_dataset(a.max_k, a.val_per_k, a.seed, keys=R.KEYS_TRAIN)
    seen = {s.delivery_id for s in tr}
    cal = R.build_dataset(a.max_k, a.val_per_k, a.seed + 999, exclude=seen, keys=R.KEYS_CAL)
    seen |= {s.delivery_id for s in cal}
    te = R.build_dataset(a.max_k, a.val_per_k, a.seed + 1777, exclude=seen, keys=R.KEYS_TEST)

    print(f"  {'身分集合':<14s} {'凍結 thr':>9s} {'oracle 上界':>11s} {'落差':>7s} "
          f"{'>0.8 的 step':>12s} {'其中 acc':>9s}")
    out = {}
    for nm, ds, ks in (("TRAIN", tr, R.KEYS_TRAIN), ("CAL", cal, R.KEYS_CAL),
                       ("TEST", te, R.KEYS_TEST)):
        rng = random.Random(a.seed + 99)
        sc, gd, hard = [], [], []
        with torch.no_grad():
            for s in ds:
                pool, lat, chain, tgt, hit, _ = R.build_pool(
                    s, rng, missing=rng.random() < a.p_missing, keys=ks)
                addrs = enc(torch.stack([sp_e[g] for g in pool]),
                            torch.stack([sp_m[g] for g in pool])).unsqueeze(0)
                q = enc(torch.stack([sp_e[g] for g in chain]),
                        torch.stack([sp_m[g] for g in chain])).unsqueeze(0)
                logits, sup = ret(None, addrs, query=q)
                tgt_t = torch.tensor(tgt, device=DEVICE)
                cos = logits[0] / ret.log_temp.exp()
                for j in range(cos.size(0)):
                    row = cos[j].clone()
                    if tgt_t[j] >= 0:
                        row[tgt_t[j]] = -2.0
                    hard.append(float(row.max()))
                sc.append(sup[0]); gd.append(torch.tensor(hit, device=DEVICE))
        sc = torch.cat(sc); gd = torch.cat(gd)
        frozen = ((sc > thr) == gd).float().mean().item()
        oracle = best_acc(sc, gd)
        hd = torch.tensor(hard, device=DEVICE)
        sel = hd > 0.8
        hacc = (((sc > thr) == gd).float()[sel].mean().item() if sel.any() else float("nan"))
        print(f"  {nm:<14s} {frozen:>9.1%} {oracle:>11.1%} {oracle-frozen:>7.1%} "
              f"{int(sel.sum()):>12d} {hacc:>9.1%}")
        out[nm] = {"frozen": frozen, "oracle": oracle, "n_hard": int(sel.sum()),
                   "hard_acc": hacc}

    print("\n  判讀：")
    print("    oracle ≈ 凍結  → score 與 threshold 都沒問題，或都一樣壞")
    print("    oracle ≫ 凍結  → **score 分得開，是 threshold 沒轉移** → calibration 覆蓋問題")
    print("    oracle 也低    → **support head 根本沒學會**，是能力問題，不是校準問題")
    print("  ⚠️ oracle 欄是在各集合自己上掃出來的**上界診斷**，不可作為結果宣稱")
    json.dump(out, open(os.path.join(HERE, "results_g2c_diagnose.json"), "w"),
              indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
