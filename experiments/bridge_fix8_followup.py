"""fixed-8 的一次性 precision follow-up —— `BRIDGE_PREREG_capacity.md` v4。

**這不是把 `n=250` 的 gate 改判 PASS。** 原 run 永遠記 92.4%／未達可判性。
本檔只解決解析度不足，且**採用比原 gate 更高的證據標準**：
**單側 95% Clopper–Pearson 下限 ≥ 95% 才算 PASS。**

規則（跑前鎖死）：同一 frozen checkpoint、**全新 seed 的 test episodes**、
**只看一次到 n=1000**、無中途停看、不再追加、無論結果照報。
"""
import json
import os
import random
import sys

import torch
from scipy.stats import beta

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import bridge_latent_schema as S
import bridge_renderer as B
from bridge_latent_gate import gen
from bridge_train_core import DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
N_EVAL, GATE_N, SEED = 1000, 8, 990601        # 全部鎖死


def cp_lower(k, n, alpha=0.05):
    """單側 95% Clopper–Pearson 下限。"""
    return 0.0 if k == 0 else float(beta.ppf(alpha, k, n - k + 1))


@torch.no_grad()
def main():
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    blob = torch.load(os.path.join(HERE, "bridge_core_fix8.pth"), map_location="cpu")
    arch = dict(blob["arch"])
    cfg = MiniMindConfig(vocab_size=blob["vocab"], **arch)
    m = MiniMindForCausalLM(cfg).to(DEVICE).eval()
    m.load_state_dict(blob["model"])
    loops = arch["num_loops"]
    proj = S.CarrierProj(arch["hidden_size"]).to(DEVICE)
    proj.scale.fill_(blob["carrier_scale"])

    print("  fixed-8 precision follow-up（prereg v4，一次性、事後提出）")
    print(f"  frozen {blob['step']} 步；**全新 seed={SEED}**；n={N_EVAL}；只看 j=0、n_carrier={GATE_N}")
    print("  ⚠️ 這**不是**把 n=250 的 gate 改判 PASS；原 run 永遠記 92.4%／未達可判性")
    print("  ⚠️ PASS 門檻採**更高證據標準**：單側 95% CP 下限 >= 95%\n")

    rng = random.Random(SEED)
    ok = fl = so = sc = 0
    for i in range(N_EVAL):
        ep = B.make_episode(rng, 0, n_fact=GATE_N)
        order = list(range(GATE_N)); rng.shuffle(order)
        lat, _ = S.episode_latents2(ep, order)
        mc = proj(lat.to(DEVICE)).unsqueeze(0)
        ids = torch.tensor(tok(tok.bos_token + S.render_latent(ep)[0],
                               add_special_tokens=False).input_ids)
        ok += int(gen(m, tok, ids, loops, mc) == ep.answer)
        fl += int(gen(m, tok, ids, loops, torch.zeros_like(mc)) == ep.answer)
        # value_shuffle（選擇是否仍由 address 驅動）
        perm = list(range(GATE_N))
        while any(perm[t] == t for t in range(GATE_N)):
            rng.shuffle(perm)
        lat2 = lat.clone(); v0 = lat[:, S.ADDR_DIM].clone()
        for t in range(GATE_N):
            lat2[t, S.ADDR_DIM] = v0[perm[t]]
        cf = B.val_str(ep.facts[order[perm[order.index(ep.ask_idx[0])]]][2])
        pred = gen(m, tok, ids, loops, proj(lat2.to(DEVICE)).unsqueeze(0))
        so += int(pred == ep.answer); sc += int(pred == cf)
        if (i + 1) % 250 == 0:
            print(f"    ...{i+1}/{N_EVAL}", flush=True)

    lo = cp_lower(ok, N_EVAL)
    print(f"\n  latent  {ok}/{N_EVAL} = {ok/N_EVAL:.2%}   單側 95% CP 下限 = {lo:.2%}")
    print(f"  floor   {fl}/{N_EVAL} = {fl/N_EVAL:.2%}")
    print(f"  value_shuffle  orig {so/N_EVAL:.1%}   cf {sc/N_EVAL:.1%}")
    v = ("**fixed-8 capability PASS**（下限 >= 95%）" if lo >= 0.95 else
         ("**改善但 gate 未封**（點估計 >= 95%，下限未達）" if ok / N_EVAL >= 0.95 else
          "**未達門檻 → 依 v4-3，R2a 容量線暫停**"))
    print(f"  → {v}")
    print("\n  ⚠️ 過了也只證明 fixed-8，**不代表**變動 n 的 R2a。")
    json.dump({"prereg": "BRIDGE_PREREG_capacity.md v4", "n": N_EVAL, "seed": SEED,
               "gate_n": GATE_N, "latent": [ok, N_EVAL], "cp_lower": lo,
               "floor": [fl, N_EVAL], "vs_orig": [so, N_EVAL], "vs_cf": [sc, N_EVAL],
               "verdict": v},
              open(os.path.join(HERE, "results_bridge_fix8_followup.json"), "w"),
              indent=2, ensure_ascii=False)
    print("  -> results_bridge_fix8_followup.json")


if __name__ == "__main__":
    main()
