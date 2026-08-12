"""mixed-name **compositional-generalization** gate —— 2×2 四格全報（Codex [151]）。

| | easy（全不同名） | hard（目標有同名兄弟） |
|---|---|---|
| **seen 組合** | | |
| **held-out 組合** | | |

**門檻不放寬：四格的點估計都必須 >=95%。** 另報 Wilson CI，
**但不得用 CI 下限替代門檻**。任一格未過 → **gate FAIL**，
不讀別格、不開機制假說、不調配方。

⚠️ held-out 的 hard 格：**目標用 held-out 組合、同名兄弟用 seen 組合** ——
   split 每個 name 只保留 1 個組合出去，held-out 集合內部不存在同名配對。
   這才是組合泛化：見過 `anna a`，現在要處理沒見過的 `anna b`。
"""
import json, os, random, sys
import torch
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer
import bridge_latent_schema as S
import bridge_renderer as B
from bridge_latent_gate import gen, wilson
from bridge_train_core import DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))


def parse(s):
    p = s.split()
    return int(p[0]) * 10 + int(p[1]) if (
        len(p) == 2 and all(len(x) == 1 and x.isdigit() for x in p)) else None


def build(rng, tgt, sib_or_dis):
    """n=2 的 episode：target ＋ 一條 distractor（同名兄弟或不同名）。"""
    tv = rng.randint(B.VMIN, B.VMAX)
    dv = rng.randint(B.VMIN, B.VMAX)
    while dv == tv:
        dv = rng.randint(B.VMIN, B.VMAX)
    facts = [(tgt[0], tgt[1], tv), (sib_or_dis[0], sib_or_dis[1], dv)]
    q = f"{tgt[0]} {B.ATTRS[tgt[1]]} 是 多少"
    return B.Episode(facts, q, B.val_str(tv), [0], 0), tv, dv


@torch.no_grad()
def main(core="bridge_core_ho.pth", n=250):
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
    print(f"  compositional-generalization gate   {core} step={blob['step']}  n={n}/格")
    print(f"  held-out {len(ho)}/48；**四格點估計都須 >=95%**，任一未過即 gate FAIL\n")
    print(f"  {'組合':>8s} {'stratum':>6s} {'target':>18s} {'distractor':>10s} "
          f"{'third':>8s} {'判定':>8s}")

    res, fails = {}, []
    for src, srcname in ((seen, "seen"), (sorted(ho), "held-out")):
        for hard in (False, True):
            rng = random.Random(88000 + len(srcname) + int(hard))
            cnt = {"target": 0, "dis": 0, "third": 0}
            for _ in range(n):
                tgt = src[rng.randrange(len(src))]
                if hard:
                    # 同名兄弟：held-out 內部沒有同名配對，故兄弟一律從**全集**取
                    sibs = [p for p in allp if p[0] == tgt[0] and p != tgt]
                    dis = sibs[rng.randrange(len(sibs))]
                else:
                    others = [p for p in src if p[0] != tgt[0]]
                    dis = others[rng.randrange(len(others))]
                ep, tv, dv = build(rng, tgt, dis)
                order = [0, 1]; rng.shuffle(order)
                lat, _ = S.episode_latents2(ep, order)
                ids = torch.tensor(tok(tok.bos_token + S.render_latent(ep)[0],
                                       add_special_tokens=False).input_ids)
                p = parse(gen(m, tok, ids, loops,
                              proj(lat.to(DEVICE)).unsqueeze(0)))
                cnt["target" if p == tv else "dis" if p == dv else "third"] += 1
            acc = cnt["target"] / n
            lo, hi = wilson(cnt["target"], n)
            ok = acc >= 0.95
            if not ok:
                fails.append(f"{srcname}/{'hard' if hard else 'easy'}")
            print(f"  {srcname:>8s} {'hard' if hard else 'easy':>6s} "
                  f"{acc:>9.1%}[{lo:.0%},{hi:.0%}] {cnt['dis']/n:>10.1%} "
                  f"{cnt['third']/n:>8.1%} {'✓' if ok else '**未過**':>8s}")
            res[f"{srcname}|{'hard' if hard else 'easy'}"] = {"counts": cnt, "n": n}

    print()
    if fails:
        print(f"  → **mixed-name／compositional-generalization gate FAIL**（{', '.join(fails)}）")
        print("    依 prereg：不讀別格的漂亮數字、不開機制假說、不調配方。")
        print("    結論停在「目前 core／訓練預算不支援 mixed-name」，另立新 core 規格。")
    else:
        print("  → **四格全過**，mixed-name 能力成立，可進入下一個 retrieval 軸。")
    json.dump({"core": core, "n": n, "heldout": sorted(map(list, ho)),
               "cells": res, "fails": fails},
              open(os.path.join(HERE, "results_bridge_heldout.json"), "w"),
              indent=2, ensure_ascii=False)
    print("  -> results_bridge_heldout.json")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "bridge_core_ho.pth",
         int(sys.argv[2]) if len(sys.argv) > 2 else 250)
