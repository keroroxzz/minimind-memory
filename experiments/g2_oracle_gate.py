"""G2a 的 render gate：**先確認新資料管線沒有動到 delivery**（Codex 要求）。

用 **oracle 檢索**（直接給正確的 latent）跑 `zdelta`，得到 oracle-retrieval ceiling。
它應該接近 §4.25 封板的 99.0%，**但不要求數值恰等** —— pool/missing 的引入
本來就改變了樣本組成。

⚠️ **ceiling 若掉出原 gate（overall ≥95%、k1 ≥95%）就停，不訓 retriever。**
   那代表資料層本身動到了 delivery，訓練再好也解讀不了。
"""
import os
import random
import sys

import torch

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import g1_renderer as R
from g1_oracle_inline import value_positions
from g1_train import ARCH, BACKBONE, DEVICE, make_delivery
from g1_teacher_kv import greedy_override
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = "g1_L0_b5d9b10c5b.pth"
ZD = "g1_G1a_zdelta_d9a603e4e2.pth"


def main():
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    cfg = MiniMindConfig(**BACKBONE, **ARCH)
    assert cfg.use_looped_transformer and cfg.num_loops == 2
    m = MiniMindForCausalLM(cfg).to(DEVICE).eval()
    blob = torch.load(os.path.join(HERE, ZD), map_location="cpu")
    m.load_state_dict(blob["model"], strict=False)
    dl = make_delivery("zdelta", BACKBONE["num_hidden_layers"], ARCH["num_loops"],
                       os.path.join(HERE, "g1_renderer_artifact.json"))
    dl.load_state_dict(blob["delivery"]); dl.eval()
    for p in list(m.parameters()) + list(dl.parameters()):
        p.requires_grad_(False)
    print(f"  core+zdelta 皆凍結（{ZD}）")

    tr = R.build_dataset(4, 5000, 42)
    va = R.build_dataset(4, 100, 42 + 999, exclude={s.delivery_id for s in tr})
    print(f"  val {len(va)}（{R.delivery_checksum(va)}）\n")

    from collections import defaultdict
    res = defaultdict(lambda: [0, 0])
    rng = random.Random(2026)
    nl = BACKBONE["num_hidden_layers"] * ARCH["num_loops"]
    with torch.no_grad():
        for s in va:
            # pool 只影響 retrieve；oracle 直接用正確 latent，故 pool 不進 delivery
            R.build_pool(s, random.Random(rng.randrange(10 ** 9)), missing=False)
            _, b_ids, pos = value_positions(tok, s)
            lat = R.resolve_chain(s).unsqueeze(0).to(DEVICE)
            box = {"i": 0}

            def fn(xk, xv, _lat=lat, _pos=pos.to(DEVICE)):
                i = box["i"]; box["i"] = (i + 1) % nl
                k, v = dl(i, _lat, xk[:, _pos], xv[:, _pos])
                return _pos, k, v

            g = greedy_override(m, tok, b_ids, fn, ARCH["num_loops"])
            r = res[s.k]; r[1] += 1; r[0] += (g == R.render_L0(s)[1])

    print("  oracle-retrieval ceiling（render gate）")
    for k in sorted(res):
        print(f"    k={k}  {res[k][0]/res[k][1]:6.1%}  (n={res[k][1]})")
    ov = sum(v[0] for v in res.values()) / sum(v[1] for v in res.values())
    k1 = res[1][0] / res[1][1]
    print(f"  整體 {ov:.1%}   （封板時的 zdelta：99.0%）")
    ok = ov >= 0.95 and k1 >= 0.95
    print(f"\n  {'✅ 通過 render gate，可以訓 retriever' if ok else '❌ 掉出 gate —— 停，先查資料層'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
