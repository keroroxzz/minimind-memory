"""G7b: dose-response causal test of the carrier-key superposition. Frozen, no training.

G7a's rho = -0.542 is **correlational** and only a necessary condition. This moves
the contamination ratio directly while holding z, slot, K_native, position and every
renderer normalisation fixed -- a cleaner first causal intervention than swapping the
placeholder token, which would change several things at once.

Three arms, all pre-locked in `g7b_prereg.json` before this ran:

    key_scale   K' = K_nat + s*ok ,  V' = V_nat + ov      the causal test
    val_scale   K' = K_nat + ok   ,  V' = V_nat + s*ov    energy control, other branch
    key_orth    K' = K_nat + R(ok),  V' = V_nat + ov      same norm, random direction

`s = 0` is the native-dot control. If superposition is causal, `key_scale` shows a
**monotone dose-response** and the other two do not. If `val_scale` shows the same
slope, the effect is total-KV energy rather than key contamination specifically, and
the causal claim does not stand.
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
from model.memory_module import PERM_N, perm_to_latent

HERE = os.path.dirname(os.path.abspath(__file__))
IDENT = list(range(PERM_N))
NL = BACKBONE["num_hidden_layers"] * ARCH["num_loops"]
PRE = json.load(open(os.path.join(HERE, "g7b_prereg.json")))
SCALES = PRE["scales_pre_locked"]


def orth_like(d, g):
    """A random tensor with the same norm as `d`, orthogonal to it."""
    r = torch.randn(d.shape, generator=g, device=d.device, dtype=d.dtype)
    flat_d, flat_r = d.reshape(-1), r.reshape(-1)
    flat_r -= flat_d * (flat_r @ flat_d) / flat_d.dot(flat_d).clamp_min(1e-12)
    return (flat_r / flat_r.norm().clamp_min(1e-12) * flat_d.norm()).view_as(d)


@torch.no_grad()
def run_episode(m, tok, dl, slots, deliv, gold, arm, s, g):
    """Teacher-forced margins + carrier stats under one (arm, scale)."""
    prompt = G5B.render_slots(IDENT, slots)
    pids = tok(tok.bos_token + prompt, add_special_tokens=False).input_ids
    full = tok(tok.bos_token + prompt + R._nums(gold), add_special_tokens=False).input_ids
    ids = torch.tensor(full).unsqueeze(0).to(DEVICE)
    _, pos = G5B.slot_positions(tok, IDENT, slots)
    pos = pos.to(DEVICE)
    lat = torch.stack([perm_to_latent(v) for v in deliv]).to(DEVICE).unsqueeze(0)
    stats, box = {"cos": [], "rat": []}, {"i": 0}

    def fn(xk, xv, _l=lat, _p=pos):
        i = box["i"]; box["i"] = (i + 1) % NL
        k, v = dl(i, _l, xk[:, _p], xv[:, _p])
        kn, vn = xk[:, _p], xv[:, _p]
        ok, ov = k - kn, v - vn                      # isolate the two deltas
        if arm == "key_scale":
            ok = s * ok
        elif arm == "val_scale":
            ov = s * ov
        elif arm == "key_orth":
            ok = orth_like(ok, g)
        k2, v2 = kn + ok, vn + ov
        cs = torch.nn.functional.cosine_similarity(
            k2.reshape(k2.shape[0], k2.shape[1], -1),
            kn.reshape(kn.shape[0], kn.shape[1], -1), dim=-1).mean().item()
        stats["cos"].append(cs)
        stats["rat"].append((ok.norm(dim=(-2, -1)) /
                             kn.norm(dim=(-2, -1)).clamp_min(1e-9)).mean().item())
        return _p, k2, v2

    lg = m(ids, kv_override=fn).logits[0]
    md = []
    for t in range(len(pids), len(full)):
        row = lg[t-1].clone(); c = full[t]
        corr = row[c].item(); row[c] = -1e9
        md.append(corr - row.max().item())
    return (sum(stats["cos"])/len(stats["cos"]),
            sum(stats["rat"])/len(stats["rat"]), md)


def main():
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    m, dl, _, _, _, _ = G3D.load_all(tok, "g3a_writer_6820855741.pth",
                                     "results_g2b.json")
    _, perms = R.perm_splits()
    N, SEED, J = PRE["n_per_cell"], PRE["seed"], PRE["j"]

    print(f"  **frozen; nothing trained**   j={J}, N={N}/cell")
    print(f"  arms: key_scale (causal) | val_scale (energy control) "
          f"| key_orth (same norm, random direction)")
    print(f"  s=0 is the native-dot control; scales {SCALES} pre-locked\n")

    out = {}
    for cfg in ("mixed", "allph"):
        print(f"  ---- config = {cfg} " + ("(PRIMARY)" if cfg == "mixed" else "(secondary)"))
        print(f"  {'arm':<11s} {'s':>4s} {'cos(K,Kdot)':>12s} {'ratK':>7s} "
              f"{'CVaR10 dm':>10s} {'P(flip)':>8s} {'accuracy':>9s}")
        for arm in ("key_scale", "val_scale", "key_orth"):
            for s in (SCALES if arm != "key_orth" else [1.0]):
                rng = random.Random(SEED + J)
                g = torch.Generator(device=DEVICE).manual_seed(SEED)
                cosv, ratv, dms, flips, acc = [], [], [], 0, 0
                for _ in range(N):
                    slots, deliv, gold = G6A.make_item(rng, perms, J, cfg)
                    c, r, md = run_episode(m, tok, dl, slots, deliv, gold, arm, s, g)
                    mt = G7A.text_margins(m, tok, slots, deliv, gold)
                    n = min(len(md), len(mt))
                    if not n:
                        continue
                    dm = min(md[i] - mt[i] for i in range(n))
                    dms.append(dm); cosv.append(c); ratv.append(r)
                    flips += int(any(md[i] < 0 for i in range(n)))
                    lt = torch.stack([perm_to_latent(v) for v in deliv]).to(DEVICE)
                    # greedy accuracy under the same intervention
                    acc += int(_greedy(m, tok, dl, slots, lt, arm, s, g) == gold)
                srt = sorted(dms); k10 = max(1, len(srt)//10)
                cvar = sum(srt[:k10])/k10
                tag = f"{arm:<11s} {s:>4.1f}"
                print(f"  {tag} {sum(cosv)/len(cosv):>12.4f} {sum(ratv)/len(ratv):>7.3f} "
                      f"{cvar:>10.3f} {flips/len(dms):>7.1%} {acc/len(dms):>8.1%}")
                out[f"{cfg}|{arm}|{s}"] = {
                    "cos": sum(cosv)/len(cosv), "rat": sum(ratv)/len(ratv),
                    "cvar10_dm": cvar, "p_flip": flips/len(dms),
                    "accuracy": acc/len(dms), "n": len(dms)}
        print()

    def slope(cfg, arm):
        xs = [s for s in SCALES]
        ys = [out[f"{cfg}|{arm}|{s}"]["cvar10_dm"] for s in SCALES]
        n = len(xs); sx, sy = sum(xs), sum(ys)
        sxx = sum(x*x for x in xs); sxy = sum(x*y for x, y in zip(xs, ys))
        return (n*sxy - sx*sy) / max(n*sxx - sx*sx, 1e-9)

    sk, sv = slope("mixed", "key_scale"), slope("mixed", "val_scale")
    ak = slope("allph", "key_scale")
    mono = all(out[f"mixed|key_scale|{SCALES[i+1]}"]["cvar10_dm"] >=
               out[f"mixed|key_scale|{SCALES[i]}"]["cvar10_dm"] - 1e-9
               for i in range(len(SCALES)-1))
    print(f"  slope of CVaR10(dm) vs s   mixed/key_scale {sk:+.3f}   "
          f"mixed/val_scale {sv:+.3f}   allph/key_scale {ak:+.3f}")
    print(f"  monotone in mixed/key_scale: {mono}")
    if not mono:
        v = "NO DOSE-RESPONSE -> keep only G7a's correlational support; " \
            "withdraw strong causal language"
    elif abs(sk) <= abs(sv) * 1.5:
        v = "CONFOUNDED -> val_scale has a comparable slope, so this is total-KV " \
            "energy, not key contamination specifically"
    else:
        v = "DOSE-RESPONSE SUPPORTS key-specific causality (still not sufficient alone)"
    print(f"\n  pre-locked verdict: **{v}**")
    print(f"  allph slope is reported as a secondary check: a flat slope there would "
          f"corroborate\n  that allph is a different, still-unexplained mechanism.")
    json.dump({"prereg": PRE, "cells": out,
               "slope_mixed_key": sk, "slope_mixed_val": sv, "slope_allph_key": ak,
               "monotone": mono, "verdict": v},
              open(os.path.join(HERE, "results_g7b.json"), "w"), indent=2,
              ensure_ascii=False)
    print(f"  -> results_g7b.json")


@torch.no_grad()
def _greedy(m, tok, dl, slots, lat, arm, s, g):
    from g1_teacher_kv import greedy_override
    ids, pos = G5B.slot_positions(tok, IDENT, slots)
    box = {"i": 0}

    def fn(xk, xv, _p=pos.to(DEVICE)):
        i = box["i"]; box["i"] = (i + 1) % NL
        k, v = dl(i, lat.unsqueeze(0), xk[:, _p], xv[:, _p])
        kn, vn = xk[:, _p], xv[:, _p]
        ok, ov = k - kn, v - vn
        if arm == "key_scale":
            ok = s * ok
        elif arm == "val_scale":
            ov = s * ov
        elif arm == "key_orth":
            ok = orth_like(ok, g)
        return _p, kn + ok, vn + ov
    o = greedy_override(m, tok, ids, fn, ARCH["num_loops"])
    try:
        p = [int(x) for x in o.split()]
        return p if sorted(p) == IDENT else None
    except ValueError:
        return None


if __name__ == "__main__":
    main()
