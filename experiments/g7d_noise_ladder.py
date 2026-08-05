"""G7d: bounded noise ladder -- can a frozen core consume a lossy-but-DECODABLE latent?

G7c was INVALID: its operators could not produce a lossy-but-decodable regime at all
(uniform quantisation is a no-op on a 0/1 matrix; coordinate erasure and projection
destroy a 25-dim one-hot immediately, because it is the *minimal* lossless code for a
permutation and carries no redundancy). This is a NEW spec with its own artifact; the
G7c run is not renamed.

Additive Gaussian noise gives the missing regime: precision degrades continuously while
the arg-max stays decodable, so `fidelity 100% but accuracy down` -- the question this
branch exists to ask -- becomes reachable.

Two disciplines carried over from the review, both pre-locked in `g7d_prereg.json`:

  **operator range preflight** runs FIRST and AUTO-STOPS on a degenerate ladder. After
  three invalid specifications we no longer rely on a smoke run to notice.

  **sigma is chosen only on an independent calibration stream**, by fixed bisection on
  decode fidelity, then locked. The test uses fresh episodes *and* fresh noise draws.
  Choosing sigma by looking at task accuracy is forbidden.

A fidelity target defines arg-max decodability only, so every cell also reports
continuous cosine/L2 distortion, and a sigma=0 oracle row is kept.
"""
import json
import math
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
from model.memory_module import PERM_N, latent_to_perm, perm_to_latent

HERE = os.path.dirname(os.path.abspath(__file__))
IDENT = list(range(PERM_N))
NL = BACKBONE["num_hidden_layers"] * ARCH["num_loops"]
PRE = json.load(open(os.path.join(HERE, "g7d_prereg.json")))


def noisy(z, sigma, g):
    return z + torch.randn(z.shape, generator=g, device=z.device, dtype=z.dtype) * sigma


def fidelity_at(sigma, perms, n, seed):
    """Decode top-1 fidelity + continuous distortion on the CALIBRATION stream only."""
    g = torch.Generator().manual_seed(seed)
    rng = random.Random(seed)
    ok, cos, l2 = 0, 0.0, 0.0
    for _ in range(n):
        p = list(perms[rng.randrange(len(perms))])
        z = perm_to_latent(p)
        zn = noisy(z, sigma, g)
        ok += int(latent_to_perm(zn) == p)
        cos += torch.nn.functional.cosine_similarity(z.flatten(), zn.flatten(), dim=0).item()
        l2 += (zn - z).norm().item()
    return ok/n, cos/n, l2/n


def preflight(perms, targets, n, seed):
    """Fixed bisection for sigma at each fidelity target. AUTO-STOPS on degeneracy."""
    print(f"  ---- operator range preflight (calibration stream only, n={n})")
    print(f"  {'target':>7s} {'sigma':>8s} {'fidelity':>9s} {'cos':>7s} {'L2':>7s}")
    out = {}
    for t in targets:
        if t >= 1.0:
            s = 0.0
            f, c, l = fidelity_at(0.0, perms, n, seed)
        else:
            lo, hi = 0.0, 4.0
            for _ in range(28):
                mid = (lo + hi) / 2
                f, _, _ = fidelity_at(mid, perms, n, seed)
                if f > t:
                    lo = mid
                else:
                    hi = mid
            s = (lo + hi) / 2
            f, c, l = fidelity_at(s, perms, n, seed)
        out[t] = {"sigma": s, "fidelity": f, "cos": c, "l2": l}
        print(f"  {t:>7.0%} {s:>8.4f} {f:>9.1%} {c:>7.3f} {l:>7.3f}")
    fids = [v["fidelity"] for v in out.values()]
    degenerate = (max(fids) - min(fids) < 0.02) or all(f < 0.02 for f in fids)
    print(f"  ladder spread {min(fids):.1%} .. {max(fids):.1%}   "
          + ("**DEGENERATE -> AUTO-STOP**" if degenerate else "usable"))
    return out, degenerate


@torch.no_grad()
def cell(m, tok, dl, perms, sigma, j, cfg, n, seed):
    rng = random.Random(seed + j)
    g = torch.Generator().manual_seed(seed + 7919*j)      # FRESH noise draws
    dms, flips, acc, fid, tex_ok, cosv = [], 0, 0, 0, 0, 0.0
    for _ in range(n):
        slots, deliv, gold = G6A.make_item(rng, perms, j, cfg)
        raw = torch.stack([perm_to_latent(v) for v in deliv])
        z = noisy(raw, sigma, g)
        fid += int(all(latent_to_perm(z[i]) == list(deliv[i]) for i in range(len(deliv))))
        cosv += torch.nn.functional.cosine_similarity(
            raw.flatten(), z.flatten(), dim=0).item()

        lat = z.to(DEVICE)
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
            "cos": cosv/len(dms), "text_ceiling": tex_ok/len(dms), "n": len(dms)}


def main():
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    _, perms = R.perm_splits()
    tgt = PRE["sigma_selection"]["fidelity_targets"]

    lad, degen = preflight(perms, tgt, PRE["n_calibration"], PRE["seed_calibration"])
    if degen:
        print(f"\n  **AUTO-STOP before the formal run.** Per the pre-registered exit "
              f"condition,\n  seal branch (1) and proceed to (3) state separation.")
        json.dump({"prereg": PRE, "preflight": lad, "verdict": "AUTO-STOP degenerate"},
                  open(os.path.join(HERE, "results_g7d.json"), "w"), indent=2)
        return
    print(f"  sigma LOCKED; the test below uses fresh episodes and fresh noise draws.\n")

    m, dl, _, _, _, _ = G3D.load_all(tok, "g3a_writer_6820855741.pth",
                                     "results_g2b.json")
    N, SEED = PRE["n_per_cell"], PRE["seed_test"]
    out = {}
    for cfg in PRE["configs"]:
        print(f"  ---- config = {cfg}")
        print(f"  {'target':>7s} {'sigma':>7s} " +
              " ".join(f"{'j='+str(j):>16s}" for j in PRE["j_values"]) +
              f" {'fidelity':>9s} {'cos':>6s}")
        for t in tgt:
            s = lad[t]["sigma"]
            row, fid, cs = [], None, None
            for j in PRE["j_values"]:
                c = cell(m, tok, dl, perms, s, j, cfg, N, SEED)
                out[f"{cfg}|t{t}|j{j}"] = c
                row.append(c); fid, cs = c["schema_fidelity"], c["cos"]
            cells = " ".join(f"{c['accuracy']:6.1%}/{c['cvar10_dm']:8.2f}" for c in row)
            cens = any(c["text_ceiling"] < .95 for c in row)
            print(f"  {t:>7.0%} {s:>7.4f} {cells} {fid:>9.1%} {cs:>6.3f}"
                  + ("  **censored**" if cens else ""))
        print()

    print(f"  cells are  accuracy / CVaR10(dm)\n")
    fusion = [k for k, v in out.items()
              if v["schema_fidelity"] >= 0.999 and v["accuracy"] < 0.90]
    schema = [k for k, v in out.items() if v["schema_fidelity"] < 0.999]
    print(f"  **fusion effect** (fidelity 100%, accuracy <90%): {len(fusion)} cells")
    print(f"  **schema capacity effect** (fidelity itself down): {len(schema)} cells")
    print(f"\n  caveat, pre-locked: the latent is one-hot, so a low-fidelity cell is"
          f"\n  **code corruption stress**, not natural semantic compression.")
    json.dump({"prereg": PRE, "preflight": lad, "cells": out,
               "fusion_cells": fusion, "schema_cells": schema},
              open(os.path.join(HERE, "results_g7d.json"), "w"), indent=2,
              ensure_ascii=False)
    print(f"  -> results_g7d.json")


if __name__ == "__main__":
    main()
