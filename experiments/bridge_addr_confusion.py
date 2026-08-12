"""**診斷,不是新 milestone。** `easy` 的 distractor 錯誤是不是 address 碼太像造成的?

§4.61 的 `seen/easy` 有 15.2% 答成 distractor 的值。若這是 address 分辨力不足,
那 `|cos(φ(tgt), φ(dis))|` 在**錯誤 trial** 上應該系統性高於**正確 trial**。
若兩者分佈重疊,`ADDR_DIM 16→32` 這條路當場排除 —— 不必訓練就能判。

只描述,不改任何配方;沿用 `bridge_heldout_eval.py` 的凍結 core 與同一組 stratum。
"""
import json, os, random, sys
import torch
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer
import bridge_latent_schema as S
import bridge_renderer as B
from bridge_latent_gate import gen
from bridge_heldout_eval import build, parse
from bridge_train_core import DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))


def summ(xs):
    if not xs:
        return "        n=0        "
    t = torch.tensor(xs)
    return f"n={len(xs):<4d} 均{t.mean():.3f} 中{t.median():.3f} 最大{t.max():.3f}"


@torch.no_grad()
def main(core="bridge_core_ho.pth", n=400):
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    blob = torch.load(os.path.join(HERE, core), map_location="cpu")
    arch = dict(blob["arch"])
    m = MiniMindForCausalLM(MiniMindConfig(vocab_size=blob["vocab"], **arch)).to(DEVICE).eval()
    m.load_state_dict(blob["model"])
    loops = arch["num_loops"]
    proj = S.CarrierProj(arch["hidden_size"]).to(DEVICE)
    proj.scale.fill_(blob["carrier_scale"])

    ho = B.heldout_split()
    allp = [(nm, ai) for nm in B.NAMES for ai in range(len(B.ATTRS))]
    seen = [p for p in allp if p not in ho]

    # 全體 48 個 descriptor 兩兩 |cos| 的參考分佈
    flat = torch.stack([S.phi(*p) for p in allp])
    C = (flat @ flat.T).abs()
    C.fill_diagonal_(0)
    off = C[C > 0]
    print(f"  φ 全體兩兩 |cos|：中位 {off.median():.3f} 最大 {off.max():.3f}\n")
    print(f"  {core} step={blob['step']}  easy stratum only  n={n}/組")
    print("  問：答錯成 distractor 的 trial，其 |cos(tgt,dis)| 是否高於答對的？\n")

    res = {}
    for src, srcname in ((seen, "seen"), (sorted(ho), "held-out")):
        rng = random.Random(91000 + len(srcname))
        cos = {"target": [], "dis": [], "third": []}
        same_attr = {"target": 0, "dis": 0, "third": 0}
        for _ in range(n):
            tgt = src[rng.randrange(len(src))]
            others = [p for p in src if p[0] != tgt[0]]
            dis = others[rng.randrange(len(others))]
            ep, tv, dv = build(rng, tgt, dis)
            order = [0, 1]; rng.shuffle(order)
            lat, _ = S.episode_latents2(ep, order)
            ids = torch.tensor(tok(tok.bos_token + S.render_latent(ep)[0],
                                   add_special_tokens=False).input_ids)
            p = parse(gen(m, tok, ids, loops, proj(lat.to(DEVICE)).unsqueeze(0)))
            k = "target" if p == tv else "dis" if p == dv else "third"
            cos[k].append(float(torch.dot(S.phi(*tgt), S.phi(*dis)).abs()))
            same_attr[k] += int(tgt[1] == dis[1])
        print(f"  [{srcname}]")
        for k in ("target", "dis", "third"):
            sa = f"同attr {same_attr[k]}/{len(cos[k])}" if cos[k] else ""
            print(f"    {k:>7s} |cos| {summ(cos[k])}   {sa}")
        # 條件正確率：target/distractor 的 attr 相同 vs 不同
        nsa = sum(same_attr.values())
        print(f"    → 同 attr  {same_attr['target']}/{nsa} = {same_attr['target']/max(nsa,1):.1%}"
              f"   異 attr  {len(cos['target'])-same_attr['target']}/{n-nsa}"
              f" = {(len(cos['target'])-same_attr['target'])/max(n-nsa,1):.1%}")
        res[srcname] = {k: {"cos": cos[k], "same_attr": same_attr[k]} for k in cos}
        res[srcname]["cond_acc"] = {"same_attr": [same_attr["target"], nsa],
                                    "diff_attr": [len(cos["target"]) - same_attr["target"],
                                                  n - nsa]}

    print("\n  判讀：`dis` 與 `target` 的 |cos| 分佈若重疊 → address 分辨力**不是**主因，")
    print("        `ADDR_DIM 16→32` 排除。若 `dis` 明顯偏高 → 加寬 address 有依據。")
    print("        同時看 `同attr` 比例：若 `dis` 的同 attr 比例明顯高，主因是 attr 欄位而非 addr。")
    json.dump({"core": core, "n": n, "phi_offdiag_median": float(off.median()),
               "phi_offdiag_max": float(off.max()), "cells": res},
              open(os.path.join(HERE, "results_bridge_addr_confusion.json"), "w"),
              indent=2, ensure_ascii=False)
    print("  -> results_bridge_addr_confusion.json")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "bridge_core_ho.pth",
         int(sys.argv[2]) if len(sys.argv) > 2 else 400)
