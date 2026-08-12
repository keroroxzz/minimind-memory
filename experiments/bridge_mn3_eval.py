"""`EXP-MN3` gate —— **六格全報**（`BRIDGE_PREREG_retrieval.md` 的 EXP-MN3 節，Codex [156] 鎖定）。

| | `R_addr`（異名／同 attr） | `R_attr`（同名／異 attr） | `R_both`（n=3，兩者各一） |
|---|---|---|---|
| **seen** | | | |
| **held-out** | | | |

**六格點估計都必須 ≥95%，全過才 PASS。** 另報 Wilson CI，**不得用 CI 下限替代門檻**。
任一格未過 → **封存這條 fixed-schema mixed-name 線**，不自動接 width／address／loss 實驗。

**佐證（不可救 FAIL）**：`R_addr` 的凍結 `swap_addr` fidelity check，
n=200，distractor-output ≥90%。它證明 routing 確實走 addr，
**但一格 accuracy 未過就是 FAIL**，這個數字不能拿來抵銷。
"""
import json, os, random, sys
import torch
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer
import bridge_latent_schema as S
import bridge_renderer as B
from bridge_latent_gate import gen, wilson
from bridge_heldout_eval import parse
from bridge_train_core import DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
CELLS = ("R_addr", "R_attr", "R_both")


@torch.no_grad()
def run(m, tok, proj, loops, rng, tgt_pool, cell, n, swap_addr=False, pool=None):
    """回傳 (答對數, 答成 distractor 數, n)。`swap_addr` 只用於 fidelity 佐證。

    `tgt_pool` = target 抽樣範圍（seen 或 held-out）；`pool` = distractor 範圍（全集）。
    held-out 內部沒有同名配對，所以 distractor **必須**能取自全集。
    """
    ok = bad = 0
    for _ in range(n):
        nf, rel = (3, None) if cell == "R_both" else (2, cell)
        ep = B.make_episode_rel(rng, 0, nf, relation=rel, pool=pool, tgt_pool=tgt_pool)
        t = ep.ask_idx[0]
        tv = ep.facts[t][2]
        order = list(range(nf)); rng.shuffle(order)
        lat, _ = S.episode_latents2(ep, order)
        if swap_addr:
            # 只在 n=2 有定義：把兩條 carrier 的 addr 欄互換，分佈逐元素不變
            assert nf == 2
            lat = lat.clone()
            lat[:, :S.ADDR_DIM] = lat[:, :S.ADDR_DIM].flip(0)
        ids = torch.tensor(tok(tok.bos_token + S.render_latent(ep)[0],
                               add_special_tokens=False).input_ids)
        p = parse(gen(m, tok, ids, loops, proj(lat.to(DEVICE)).unsqueeze(0)))
        ok += int(p == tv)
        bad += int(any(p == v for i, (_, _, v) in enumerate(ep.facts) if i != t))
    return ok, bad, n


def main(core="bridge_core_mn3.pth", n=250):
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
    print(f"  EXP-MN3 gate   {core} step={blob['step']}  n={n}/格")
    print(f"  held-out {len(ho)}/48；**六格點估計都須 ≥95%**，任一未過即封存此線")
    print("  ⚠️ held-out 格：**目標**取自 held-out，distractor 取自全集 ——")
    print("     held-out 集合內部不存在同名配對，R_attr 在純 held-out pool 裡建不出來。\n")
    print(f"  {'組合':>8s} {'R_addr':>18s} {'R_attr':>18s} {'R_both':>18s}")

    res, fails = {}, []
    for src, srcname in ((seen, "seen"), (sorted(ho), "held-out")):
        row = []
        for cell in CELLS:
            rng = random.Random(95000 + len(srcname) + CELLS.index(cell))
            ok, bad, N = run(m, tok, proj, loops, rng, src, cell, n, pool=allp)
            lo, hi = wilson(ok, N)
            if ok / N < 0.95:
                fails.append(f"{srcname}/{cell}")
            row.append(f"{ok/N:>9.1%}[{lo:.0%},{hi:.0%}]")
            res[f"{srcname}|{cell}"] = {"ok": ok, "dis": bad, "n": N}
        print(f"  {srcname:>8s} " + " ".join(row))

    # 佐證：R_addr 的 swap_addr fidelity（**不可救 FAIL**）
    rng = random.Random(96000)
    ok, bad, N = run(m, tok, proj, loops, rng, seen, "R_addr", 200,
                     swap_addr=True, pool=allp)
    fid = bad / N
    res["fidelity_swap_addr"] = {"dis_output": bad, "n": N, "target": ok}
    print(f"\n  佐證 `R_addr` swap_addr fidelity：distractor-output {bad}/{N} = {fid:.1%} "
          f"（門檻 ≥90%）{'✓' if fid >= 0.90 else '✗'}")
    print("    ⚠️ 這只證明 routing 走 addr；**任一 primary 格未過仍是 FAIL**，不得抵銷。")

    print()
    if fails:
        print(f"  → **EXP-MN3 gate FAIL**（{', '.join(fails)}）")
        print("    依 prereg：封存 fixed-schema mixed-name 線，不自動接 width／address／loss，")
        print("    轉回 retrieval 軸，結論須帶「不涵蓋同實體多屬性」的範圍限制。")
    else:
        print("  → **六格全過，EXP-MN3 PASS**。新 core 只是 baseline，")
        print("    舊 writer/retriever/delivery artifacts 不自動繼承，需另走相容性 staircase。")
    json.dump({"core": core, "n": n, "cells": res, "fails": fails},
              open(os.path.join(HERE, "results_bridge_mn3.json"), "w"),
              indent=2, ensure_ascii=False)
    print("  -> results_bridge_mn3.json")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "bridge_core_mn3.pth",
         int(sys.argv[2]) if len(sys.argv) > 2 else 250)
