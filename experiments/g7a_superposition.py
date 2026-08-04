"""G7a: falsify (or support) the **carrier-superposition** hypothesis. Frozen, no training.

The hypothesis (research.md 4.45 follow-up): `zdelta` writes
`K' = K_native + f(z)` onto a placeholder `.` token, so the carrier key is a
**superposition** of dot-identity and content, whereas a text value has a clean
key. Later composition steps must re-attend to that value and see a contaminated
key -- which is why delivered values compose worse than text.

**This is the fifth mechanism narrative from the implementing agent and the previous
four were all refuted.** So it is tested by a pre-locked falsifier BEFORE any repair
is attempted (`g7a_prereg.json`, written before this ran):

    direction  higher cos(K', K_dot) == more contaminated == MORE NEGATIVE dm
               => predicted correlation is NEGATIVE
    supports   Spearman rho <= -0.30   (at j=3, mixed)
    refutes    |rho| < 0.15
    otherwise  inconclusive -- and explicitly NOT a licence to proceed to a fix

A positive result is only a **necessary** condition; it never establishes sufficient
causality (Codex). The placeholder-swap repair may only be attempted afterwards, and
only with content/position/norm/token statistics matched.
"""
import json
import math
import os
import random
import sys
from collections import defaultdict

import torch

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import g1_renderer as R
import g3a_train as G3
import g3b_closure as G3B
import g3d_scale_audit as G3D
import g5b_segmented_reasoning as G5B
import g6a_zdelta_v2 as G6A
from g1_train import ARCH, BACKBONE, DEVICE
from model.memory_module import ADDR_DIM, PERM_N, perm_to_latent

HERE = os.path.dirname(os.path.abspath(__file__))
IDENT = list(range(PERM_N))
NL = BACKBONE["num_hidden_layers"] * ARCH["num_loops"]
PRE = json.load(open(os.path.join(HERE, "g7a_prereg.json")))
SUPPORT, REFUTE = -0.30, 0.15


def spearman(a, b):
    """Rank correlation without scipy (ties averaged)."""
    def rank(x):
        order = sorted(range(len(x)), key=lambda i: x[i])
        r = [0.0]*len(x); i = 0
        while i < len(order):
            j = i
            while j+1 < len(order) and x[order[j+1]] == x[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j+1):
                r[order[k]] = avg
            i = j + 1
        return r
    ra, rb = rank(a), rank(b)
    n = len(a)
    ma, mb = sum(ra)/n, sum(rb)/n
    num = sum((x-ma)*(y-mb) for x, y in zip(ra, rb))
    da = math.sqrt(sum((x-ma)**2 for x in ra))
    db = math.sqrt(sum((y-mb)**2 for y in rb))
    return num/(da*db) if da*db else float("nan")


@torch.no_grad()
def carrier_stats_and_dm(m, tok, dl, slots, deliv, gold):
    """One episode: carrier key/value contamination + teacher-forced dm.

    Both are captured in the SAME forward so they are paired by construction.
    """
    prompt = G5B.render_slots(IDENT, slots)
    pids = tok(tok.bos_token + prompt, add_special_tokens=False).input_ids
    full = tok(tok.bos_token + prompt + R._nums(gold), add_special_tokens=False).input_ids
    ids = torch.tensor(full).unsqueeze(0).to(DEVICE)
    _, pos = G5B.slot_positions(tok, IDENT, slots)
    pos = pos.to(DEVICE)
    lat = torch.stack([perm_to_latent(v) for v in deliv]).to(DEVICE).unsqueeze(0)

    grab = {"cosK": [], "cosV": [], "ratK": [], "ratV": []}
    box = {"i": 0}

    def fn(xk, xv, _l=lat, _p=pos):
        i = box["i"]; box["i"] = (i + 1) % NL
        k, v = dl(i, _l, xk[:, _p], xv[:, _p])
        kn, vn = xk[:, _p], xv[:, _p]                 # native '.' key/value
        f = lambda a, b: torch.nn.functional.cosine_similarity(
            a.reshape(a.shape[0], a.shape[1], -1),
            b.reshape(b.shape[0], b.shape[1], -1), dim=-1).mean().item()
        rn = lambda d, n: (d.norm(dim=(-2, -1)) / n.norm(dim=(-2, -1)).clamp_min(1e-9)
                           ).mean().item()
        grab["cosK"].append(f(k, kn)); grab["cosV"].append(f(v, vn))
        grab["ratK"].append(rn(k - kn, kn)); grab["ratV"].append(rn(v - vn, vn))
        return _p, k, v

    lg = m(ids, kv_override=fn).logits[0]
    dm_pos = []
    for t in range(len(pids), len(full)):
        row = lg[t-1].clone(); c = full[t]
        corr = row[c].item(); row[c] = -1e9
        dm_pos.append(corr - row.max().item())        # this is m_delivery
    avg = lambda k: sum(grab[k])/len(grab[k])
    return (avg("cosK"), avg("cosV"), avg("ratK"), avg("ratV")), dm_pos


@torch.no_grad()
def text_margins(m, tok, slots, deliv, gold):
    expl = list(deliv) + [s for s in slots if s is not None]
    prompt = G5B.render_slots(IDENT, expl)
    pids = tok(tok.bos_token + prompt, add_special_tokens=False).input_ids
    full = tok(tok.bos_token + prompt + R._nums(gold), add_special_tokens=False).input_ids
    lg = m(torch.tensor(full).unsqueeze(0).to(DEVICE)).logits[0]
    out = []
    for t in range(len(pids), len(full)):
        row = lg[t-1].clone(); c = full[t]
        corr = row[c].item(); row[c] = -1e9
        out.append(corr - row.max().item())
    return out


def main():
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    m, dl, _, _, _, _ = G3D.load_all(tok, "g3a_writer_6820855741.pth",
                                     "results_g2b.json")
    _, perms = R.perm_splits()
    N, SEED = PRE["n_per_cell"], PRE["seed"]

    print(f"  **frozen core + frozen zdelta-v1; nothing is trained**")
    print(f"  pre-locked: supports rho <= {SUPPORT}, refutes |rho| < {REFUTE}"
          f"  (g7a_prereg.json)\n")
    print(f"  {'cell':<12s} {'rho(cosK,dm)':>13s} {'rho(cosV,dm)':>13s} "
          f"{'rho(ratK,dm)':>13s} {'median dm':>10s} {'CVaR10 dm':>10s} {'text':>7s}")

    out = {}
    for j in (0, 1, 2, 3):
        for cfg in ("allph", "mixed"):
            rng = random.Random(SEED + j)
            cosK, cosV, ratK, dms, tex = [], [], [], [], []
            for _ in range(N):
                slots, deliv, gold = G6A.make_item(rng, perms, j, cfg)
                (cK, cV, rK, _rV), md = carrier_stats_and_dm(
                    m, tok, dl, slots, deliv, gold)
                mt = text_margins(m, tok, slots, deliv, gold)
                n = min(len(md), len(mt))
                if not n:
                    continue
                # one paired scalar per episode: the WORST position, since 4.47
                # showed the failure lives in the tail, not the median
                dm = min(md[i] - mt[i] for i in range(n))
                cosK.append(cK); cosV.append(cV); ratK.append(rK)
                dms.append(dm); tex.append(min(mt[:n]))
            srt = sorted(dms); k10 = max(1, len(srt)//10)
            cvar = sum(srt[:k10])/k10
            med = srt[len(srt)//2]
            # text ceiling for censoring: fraction of episodes the teacher gets right
            tok_ok = sum(1 for t in tex if t > 0)/len(tex)
            r1, r2, r3 = (spearman(cosK, dms), spearman(cosV, dms), spearman(ratK, dms))
            tag = f"j={j} {cfg}"
            print(f"  {tag:<12s} {r1:>13.3f} {r2:>13.3f} {r3:>13.3f} "
                  f"{med:>10.3f} {cvar:>10.3f} {tok_ok:>6.1%}")
            out[tag] = {"rho_cosK_dm": r1, "rho_cosV_dm": r2, "rho_ratK_dm": r3,
                        "dm_median": med, "dm_cvar10": cvar, "text_ok": tok_ok,
                        "n": len(dms), "cosK_mean": sum(cosK)/len(cosK),
                        "ratK_mean": sum(ratK)/len(ratK)}

    key = "j=3 mixed"
    rho = out[key]["rho_cosK_dm"]
    verdict = ("SUPPORTS" if rho <= SUPPORT else
               "REFUTES" if abs(rho) < REFUTE else "INCONCLUSIVE")
    print(f"\n  **primary** ({key}): Spearman rho(cos(K',K_dot), dm) = {rho:+.3f}")
    print(f"  pre-locked verdict: **{verdict}**")
    if verdict == "REFUTES":
        print(f"  -> carrier superposition is NOT the mechanism. Withdraw the")
        print(f"     hypothesis; do NOT attempt the placeholder-swap repair.")
    elif verdict == "SUPPORTS":
        print(f"  -> NECESSARY condition only. A matched placeholder variant")
        print(f"     (content/position/norm/token statistics) is required next;")
        print(f"     this does not establish sufficient causality.")
    else:
        print(f"  -> inconclusive; explicitly NOT a licence to proceed to a fix.")
    print(f"\n  context: mean cos(K',K_dot) = {out[key]['cosK_mean']:.4f}, "
          f"mean ||f(z)||/||K_nat|| = {out[key]['ratK_mean']:.4f}")
    json.dump({"prereg": PRE, "cells": out, "primary_cell": key,
               "primary_rho": rho, "verdict": verdict},
              open(os.path.join(HERE, "results_g7a.json"), "w"),
              indent=2, ensure_ascii=False)
    print(f"  -> results_g7a.json")


if __name__ == "__main__":
    main()
