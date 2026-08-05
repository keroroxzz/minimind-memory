"""G7c: can a frozen core consume a **lossy** latent? Oracle ladder, no training.

Everything from G1 to G6 stands on a 25-dim **lossless, closed** $S_5$ schema.
Natural-language facts are unbounded, heterogeneous and necessarily lossy, so
"can a frozen backbone consume a lossy representation at all" is on the critical
path -- and it is completely untested. It is also **independent** of the unresolved
composition mechanism (G7a supported only correlationally, G7b withdrew causality),
so it is not blocked by it.

Three pre-locked degradations (`g7c_prereg.json`), applied to the oracle latent
before delivery. Nothing is trained; this is a ladder, not a repair.

The key separation, also pre-locked:

    schema fidelity 100% but accuracy degrades  ->  the backbone cannot CONSUME a
                                                    lossy-but-sufficient latent
    schema fidelity drops with r                ->  the information is simply gone,
                                                    and the delivery failure says
                                                    nothing about consumption
"""
import json
import os
import random
import sys

import torch

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import g1_renderer as R
import g3d_scale_audit as G3D
import g5b_segmented_reasoning as G5B
import g6a_zdelta_v2 as G6A
import g7a_superposition as G7A
from g1_train import ARCH, BACKBONE, DEVICE
from model.memory_module import LATENT_DIM, PERM_N, latent_to_perm, perm_to_latent

HERE = os.path.dirname(os.path.abspath(__file__))
IDENT = list(range(PERM_N))
NL = BACKBONE["num_hidden_layers"] * ARCH["num_loops"]
PRE = json.load(open(os.path.join(HERE, "g7c_prereg.json")))
RS, JS = PRE["r_values"], PRE["j_values"]

_g = torch.Generator().manual_seed(PRE["seed"])
KEEP = {r: torch.randperm(LATENT_DIM, generator=_g)[:r] for r in RS}   # fixed subsets
_Q, _ = torch.linalg.qr(torch.randn(LATENT_DIM, LATENT_DIM, generator=_g))
BASIS = _Q                                                            # fixed basis


def degrade(z, op, r):
    """Pre-locked lossy operators. `r = 25` is the identity for erase/project."""
    if r >= LATENT_DIM and op != "quant":
        return z.clone()
    if op == "erase":
        out = torch.zeros_like(z); idx = KEEP[r].to(z.device)
        out[..., idx] = z[..., idx]
        return out
    if op == "project":
        B = BASIS.to(z.device, z.dtype)
        c = z @ B                       # coefficients in the fixed basis
        c[..., r:] = 0
        return c @ B.T
    if op == "quant":
        lv = max(r, 2) - 1
        return (z.clamp(0, 1) * lv).round() / lv
    raise ValueError(op)


@torch.no_grad()
def cell(m, tok, dl, perms, op, r, j, cfg, n, seed):
    rng = random.Random(seed + j)
    dms, flips, acc, fid, tex_ok = [], 0, 0, 0, 0
    for _ in range(n):
        slots, deliv, gold = G6A.make_item(rng, perms, j, cfg)
        raw = torch.stack([perm_to_latent(v) for v in deliv])
        dz = degrade(raw, op, r)
        # schema fidelity: is the permutation still recoverable from the latent?
        fid += int(all(latent_to_perm(dz[i]) == list(deliv[i]) for i in range(len(deliv))))

        lat = dz.to(DEVICE)
        prompt = G5B.render_slots(IDENT, slots)
        pids = tok(tok.bos_token + prompt, add_special_tokens=False).input_ids
        full = tok(tok.bos_token + prompt + R._nums(gold),
                   add_special_tokens=False).input_ids
        _, pos = G5B.slot_positions(tok, IDENT, slots)
        pos = pos.to(DEVICE); box = {"i": 0}

        def fn(xk, xv, _l=lat.unsqueeze(0), _p=pos):
            i = box["i"]; box["i"] = (i + 1) % NL
            k, v = dl(i, _l, xk[:, _p], xv[:, _p])
            return _p, k, v

        lg = m(torch.tensor(full).unsqueeze(0).to(DEVICE), kv_override=fn).logits[0]
        md = []
        for t in range(len(pids), len(full)):
            row = lg[t-1].clone(); c = full[t]
            corr = row[c].item(); row[c] = -1e9
            md.append(corr - row.max().item())
        mt = G7A.text_margins(m, tok, slots, deliv, gold)
        k = min(len(md), len(mt))
        if not k:
            continue
        dms.append(min(md[i] - mt[i] for i in range(k)))
        flips += int(any(md[i] < 0 for i in range(k)))
        tex_ok += int(min(mt[:k]) > 0)
        acc += int(G5B.call_core(m, tok, IDENT, slots, dl, lat) == gold)
    srt = sorted(dms); k10 = max(1, len(srt)//10)
    return {"cvar10_dm": sum(srt[:k10])/k10, "p_flip": flips/len(dms),
            "accuracy": acc/len(dms), "schema_fidelity": fid/len(dms),
            "text_ceiling": tex_ok/len(dms), "n": len(dms)}


def main():
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    m, dl, _, _, _, _ = G3D.load_all(tok, "g3a_writer_6820855741.pth",
                                     "results_g2b.json")
    _, perms = R.perm_splits()
    N, SEED = PRE["n_per_cell"], PRE["seed"]
    print(f"  **frozen core + frozen zdelta-v1; nothing trained**   N={N}/cell")
    print(f"  degradations: erase (keep r coords) | project (top-r in fixed basis) "
          f"| quant (r levels)")
    print(f"  reading: fidelity 100% but accuracy down => CONSUMPTION failure;"
          f"\n           fidelity down => information simply gone\n")

    out = {}
    for op in ("erase", "project", "quant"):
        for cfg in ("allph", "mixed"):
            print(f"  ---- {op} / {cfg}")
            print(f"  {'r':>3s} " + " ".join(f"{'j='+str(j):>16s}" for j in JS)
                  + f" {'fidelity':>9s}")
            for r in RS:
                cells, fid = [], None
                for j in JS:
                    c = cell(m, tok, dl, perms, op, r, j, cfg, N, SEED)
                    out[f"{op}|{cfg}|r{r}|j{j}"] = c
                    cells.append(c); fid = c["schema_fidelity"]
                row = " ".join(f"{c['accuracy']:6.1%}/{c['cvar10_dm']:8.2f}"
                               for c in cells)
                cens = any(c["text_ceiling"] < .95 for c in cells)
                print(f"  {r:>3d} {row} {fid:>9.1%}"
                      + ("  **censored**" if cens else ""))
            print()

    print(f"  cells are  accuracy / CVaR10(dm)\n")
    # verdict per the pre-locked reading rule, on the lossless-fidelity cells
    consume, info = [], []
    for kk, v in out.items():
        if v["schema_fidelity"] >= 0.999 and v["accuracy"] < 0.90:
            consume.append(kk)
        if v["schema_fidelity"] < 0.999:
            info.append(kk)
    print(f"  cells with **fidelity 100% but accuracy <90%** (consumption failure): "
          f"{len(consume)}")
    for kk in consume[:8]:
        print(f"      {kk}  acc {out[kk]['accuracy']:.1%}  "
              f"CVaR10 {out[kk]['cvar10_dm']:.2f}")
    print(f"  cells where the information itself was destroyed: {len(info)}")
    print(f"\n  for stage 3 (state separation): justified only if the same tail appears"
          f"\n  at lossless or mild loss; if it only breaks under strong compression,"
          f"\n  fix schema/interface capacity first.")
    json.dump({"prereg": PRE, "cells": out,
               "consumption_failures": consume, "information_failures": info},
              open(os.path.join(HERE, "results_g7c.json"), "w"), indent=2,
              ensure_ascii=False)
    print(f"  -> results_g7c.json")


if __name__ == "__main__":
    main()
