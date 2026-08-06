"""R2a 同時注入容量的評估 —— `BRIDGE_PREREG_capacity.md`，本檔只執行。

**刻意不改 `bridge_latent_gate.py` / `bridge_latent_shuffle.py`** ——
那兩支產出了 §4.55 的數字，動它們會破壞可重現性。這裡只 import 它們的 helper。

每個 `n ∈ {2,4,8,16}` 都跑：

| 量 | 用途 |
|---|---|
| `latent` 正確率 | **primary**（曲線） |
| `floor`（carrier 歸零） | 必須留地板，否則是背答案 |
| **`value_shuffle` 的 `orig`/`cf`** | **每個 n 都要**：`cf` 高才是「選擇仍有效」 |
| 逐題 `max\|cos\|` | 機制分析（**不是 gate**，Codex [140]） |
| tokens/s、VRAM | R7 成本三軸 |

判讀（prereg §4）：CI 下界 ≥90% = 容量成立到該 n；含 `1/n` = 崩潰。
**gate 只有一條**：`latent` 在 `n=2` <95% → core/render invalid，不讀後續 n。

`cf` 與 `orig` **一起塌** = **選擇失效**；只有 `orig` 塌而 `cf` 高 = 選擇仍有效。
兩者不得混報。
"""
import json
import os
import random
import sys
import time
from collections import defaultdict

import torch

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import bridge_latent_schema as S
import bridge_renderer as B
from bridge_latent_gate import gen, wilson
from bridge_shuffle_control import answer_from, true_vals
from bridge_train_core import DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
NS = (2, 4, 8, 16)


def max_cos(ep):
    """該題內 address 兩兩最大 |cos| —— 事前登記的機制分析變數。"""
    V = torch.stack([S.phi(nm, ai) for nm, ai, _ in ep.facts])
    g = (V @ V.T)
    n = len(V)
    return float(g[~torch.eye(n, dtype=bool)].abs().max())


@torch.no_grad()
def main(n=250, core="bridge_core_cap.pth", gate_n=2):
    """`gate_n` = 該 run 的 gate 格。

    ⚠️ 初版把 gate 硬編成 `n=2`。那對 R2a 正確，但對 **fixed-load 診斷**（只訓練單一 `n`）
       是**錯的 gate** —— fixed-8 的 gate 是 `n=8`，`n=2` 對它是 OOD。
       硬編會印出「gate：n=2 未通過」這種**對該 run 沒有意義卻看起來像裁決**的句子。
    """
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    blob = torch.load(os.path.join(HERE, core), map_location="cpu")
    arch = dict(blob["arch"])
    cfg = MiniMindConfig(vocab_size=blob["vocab"], **arch)
    m = MiniMindForCausalLM(cfg).to(DEVICE).eval()
    m.load_state_dict(blob["model"])
    loops = arch["num_loops"]
    proj = S.CarrierProj(arch["hidden_size"]).to(DEVICE)
    proj.scale.fill_(blob["carrier_scale"])

    print(f"  R2a 同時注入容量（prereg: BRIDGE_PREREG_capacity.md）")
    print(f"  {core}  step={blob['step']}  n={n}/格   評估 n_carrier ∈ {NS}")
    print(f"  ⚠️ 本輪最多只是 **R2a**（同時注入），**不是** store 總量的 R2\n")

    res, cosbin = {}, defaultdict(lambda: [0, 0])
    for j in (0, 1):
        print(f"  ==== j={j}")
        print(f"    {'n':>4s} {'latent':>18s} {'floor':>9s} "
              f"{'vs 1/n':>9s}  {'value_shuffle orig':>19s} {'cf':>18s}")
        for nf in NS:
            rng = random.Random(31000 + j * 97 + nf)
            ok = fl = tot = 0
            so = sc = 0
            t0 = time.time(); ntok = 0
            for _ in range(n):
                ep = B.make_episode(rng, j, n_fact=nf)
                order = list(range(nf)); rng.shuffle(order)
                lat, _ = S.episode_latents2(ep, order)
                mc = proj(lat.to(DEVICE)).unsqueeze(0)
                ids = torch.tensor(tok(tok.bos_token + S.render_latent(ep)[0],
                                       add_special_tokens=False).input_ids)
                ntok += len(ids) + nf
                good = int(gen(m, tok, ids, loops, mc) == ep.answer)
                ok += good
                fl += int(gen(m, tok, ids, loops, torch.zeros_like(mc)) == ep.answer)
                # value_shuffle（每個 n 都要跑）——值錯排、address 與 attr 留原位
                #
                # ⚠️ `cf` 對 j>=1 **必須用有效值重算整條 |x-v| 鏈**。
                #    初版直接取單一格的值，那只有 j=0 才對；j=1 的答案是兩個值的組合，
                #    取單格會讓整個 cf 欄變成無意義的數字。
                perm = list(range(nf))
                for _ in range(200):
                    rng.shuffle(perm)
                    if all(perm[t] != t for t in range(nf)):
                        break
                lat2 = lat.clone()
                vals0 = lat[:, S.ADDR_DIM].clone()
                for t in range(nf):
                    lat2[t, S.ADDR_DIM] = vals0[perm[t]]
                # row t 帶 fact order[t] 的 address，但值來自 fact order[perm[t]]
                eff = list(true_vals(ep))
                for t in range(nf):
                    eff[order[t]] = ep.facts[order[perm[t]]][2]
                cf_ans = answer_from(ep, eff)
                mc2 = proj(lat2.to(DEVICE)).unsqueeze(0)
                pred2 = gen(m, tok, ids, loops, mc2)
                so += int(pred2 == ep.answer)
                sc += int(pred2 == cf_ans)
                tot += 1
                if j == 0:
                    b = round(max_cos(ep) * 10) / 10
                    cosbin[b][0] += good; cosbin[b][1] += 1
            dt = time.time() - t0
            lo, hi = wilson(ok, tot)
            lo2, hi2 = wilson(sc, tot)
            v = ("**成立**" if lo >= 0.90 else
                 ("**崩潰（含 1/n）**" if lo <= 1 / nf <= hi else "退化"))
            print(f"    {nf:>4d} {ok/tot:>8.1%}[{lo:.0%},{hi:.0%}] {fl/tot:>8.1%} "
                  f"{v:>12s}  {so/tot:>17.1%} {sc/tot:>10.1%}[{lo2:.0%},{hi2:.0%}]")
            res[f"j{j}|n{nf}"] = {"latent": [ok, tot], "floor": [fl, tot],
                                  "vs_shuffle_orig": [so, tot], "vs_shuffle_cf": [sc, tot],
                                  "tok_per_s": ntok / dt}
        if j == 0:
            c = res[f"j0|n{gate_n}"]["latent"]
            p = c[0] / c[1]
            lo, hi = wilson(*c)
            print(f"    → gate（本 run 的訓練格 n={gate_n}）：latent {p:.1%} "
                  f"[{lo:.1%},{hi:.1%}]  "
                  + ("**通過（>=95%）**" if lo >= 0.95
                     else ("**未達可判性**（CI 含 95%）" if hi >= 0.95
                           else "**未通過（顯著 <95%）→ invalid，不讀其他 n**")))
            print(f"      其餘 n 對本 run 為 **OOD**，僅供參考、不作判準。")
        print()

    print("  ---- 機制分析：正確率 vs 該題 address 最大 |cos|（j=0，**不是 gate**）")
    for b in sorted(cosbin):
        o, t = cosbin[b]
        if t >= 20:
            print(f"    max|cos| ≈ {b:.1f}   {o/t:>6.1%}  (n={t})")
    print("\n    掉分與 max|cos| 正相關 → 支持 **address-code crowding**")
    print("    曲線平坦或無相關     → 瓶頸**不是** 16 維碼的幾何，另尋原因")

    mem = torch.cuda.max_memory_allocated() / 2**30 if DEVICE == "cuda" else 0
    print(f"\n  ---- R7 成本：peak VRAM {mem:.2f} GiB")
    for k, v in res.items():
        if k.startswith("j0"):
            print(f"    {k}  {v['tok_per_s']:.0f} tok/s")
    json.dump({"prereg": "BRIDGE_PREREG_capacity.md", "core": core, "n": n,
               "cells": res, "cosbin": {str(k): v for k, v in cosbin.items()},
               "peak_vram_gib": mem},
              open(os.path.join(HERE, "results_bridge_capacity.json"), "w"),
              indent=2, ensure_ascii=False)
    print("  -> results_bridge_capacity.json")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 250,
         sys.argv[2] if len(sys.argv) > 2 else "bridge_core_cap.pth",
         int(sys.argv[3]) if len(sys.argv) > 3 else 2)
