"""latent-only core 的 gate —— 依 `BRIDGE_PREREG_latent.md` §6 的順序,不得跳。

    1. `L0` text ceiling
    2. `latent` oracle 的 consumption ceiling（最小 n）
    3. `latent` oracle 的 n>=2 ceiling
    4. 才談 learned retriever（本檔不做）

> **任何有 latent render 的最小 n ceiling < 95%，只記 `core/render invalid`，
> 不解讀 binding，也不談 retriever。**

三個 render（同一批 episode 配對）：

| render | 文字 | 記憶 |
|---|---|---|
| `L0` | 事實寫在文字裡 | 無 |
| `latent` | **只有問句** | oracle z'（prepend carrier）|
| `floor` | **只有問句** | carrier **全零** |

`floor` 是必要的：latent 若答對而 floor 也答對，那是模型背下了答案分布，不是讀了記憶。

⚠️ `n_carrier=1` 在訓練時**從未出現**（`n_fact` 抽 2..4），所以那一格是 **OOD**，
   單獨標示、不作為 gate 的判準；gate 用**訓練分布內的最小 n（=2）**。
   這與 H3 那次「j=1 沒有 n_carrier=1 這一格」是同一類問題，處理方式一致。
"""
import json
import os
import random
import sys
from collections import defaultdict

import torch

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import bridge_latent_schema as S
import bridge_renderer as B
from bridge_train_core import ARCH, DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))


def wilson(k, n):
    if n == 0:
        return (0.0, 1.0)
    z, p = 1.96, k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, c - h), min(1.0, c + h))


@torch.no_grad()
def gen(m, tok, ids, loops, mc=None, max_new=4):
    """carrier 只在 prefill 給一次（增量解碼時已在 cache 裡，再給會重複）。"""
    kw = {"memory_carriers": mc} if mc is not None else {}
    out = m(ids.unsqueeze(0).to(DEVICE), use_cache=True, num_loops=loops, **kw)
    pkv, got = out.past_key_values, []
    for _ in range(max_new):
        nx = out.logits[:, -1].argmax(-1, keepdim=True)
        if nx.item() == tok.eos_token_id:
            break
        got.append(nx.item())
        out = m(nx, past_key_values=pkv, use_cache=True, num_loops=loops)
        pkv = out.past_key_values
    return tok.decode(got, skip_special_tokens=True).strip()


@torch.no_grad()
def main(n=250, core="bridge_core_latent.pth"):
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    blob = torch.load(os.path.join(HERE, core), map_location="cpu")
    arch = dict(blob["arch"])
    cfg = MiniMindConfig(vocab_size=blob["vocab"], **arch)
    m = MiniMindForCausalLM(cfg).to(DEVICE).eval()
    m.load_state_dict(blob["model"])
    loops = arch["num_loops"]
    proj = S.CarrierProj(arch["hidden_size"]).to(DEVICE)
    proj.scale.fill_(blob["carrier_scale"])

    print(f"  latent-only core gate（prereg: BRIDGE_PREREG_latent.md §6）")
    print(f"  {core}  step={blob['step']}  num_loops={loops}  n={n}/格")
    print(f"  z' = {blob['lat_dim']} 維（addr {blob['addr_dim']}）  "
          f"carrier_scale={blob['carrier_scale']:.3f}\n")

    ep0 = B.make_episode(random.Random(5), 0, n_fact=3)
    print(f"  L0     ：{S.render_l0(ep0)[0]}")
    print(f"  latent ：{S.render_latent(ep0)[0]}   + {len(ep0.facts)} 個 carrier\n")

    res = {}
    for j in (0, 1, 2):
        by = defaultdict(lambda: defaultdict(lambda: [0, 0]))
        for nf in (1, 2, 3, 4):
            if j >= 1 and nf < 2:
                continue                      # j>=1 的鏈需要至少 2 條 fact
            rng = random.Random(4242 + j)
            for _ in range(n):
                ep = B.make_episode(rng, j, n_fact=nf)
                order = list(range(nf)); rng.shuffle(order)
                lat, _ = S.episode_latents2(ep, order)
                mc = proj(lat.to(DEVICE)).unsqueeze(0)
                # L0
                t0 = torch.tensor(tok(tok.bos_token + S.render_l0(ep)[0],
                                      add_special_tokens=False).input_ids)
                by["L0"][nf][0] += int(gen(m, tok, t0, loops) == ep.answer)
                by["L0"][nf][1] += 1
                # latent / floor（同一句問句）
                tq = torch.tensor(tok(tok.bos_token + S.render_latent(ep)[0],
                                      add_special_tokens=False).input_ids)
                by["latent"][nf][0] += int(gen(m, tok, tq, loops, mc) == ep.answer)
                by["latent"][nf][1] += 1
                by["floor"][nf][0] += int(
                    gen(m, tok, tq, loops, torch.zeros_like(mc)) == ep.answer)
                by["floor"][nf][1] += 1

        print(f"  ==== j={j}")
        print(f"    {'render':>8s} " + "".join(f"{'n='+str(k):>16s}" for k in (1, 2, 3, 4)))
        for r in ("L0", "latent", "floor"):
            row = ""
            for nf in (1, 2, 3, 4):
                c = by[r].get(nf)
                if not c or c[1] == 0:
                    row += f"{'—':>16s}"
                else:
                    lo, hi = wilson(c[0], c[1])
                    row += f"{c[0]/c[1]:>8.1%}[{lo:.0%},{hi:.0%}]"
            print(f"    {r:>8s} " + row)
        # gate：訓練分布內的最小 n（=2）
        c = by["latent"].get(2)
        if c:
            p = c[0] / c[1]
            print(f"    → gate（訓練分布內最小 n=2）：latent {p:.1%}  "
                  + ("**通過（>=95%）**" if p >= 0.95
                     else "**未通過 → 只記 core/render invalid，不解讀 binding**"))
        res[j] = {r: {str(k): by[r][k] for k in by[r]} for r in by}
        print()

    print("  註：n_carrier=1 在訓練時從未出現（n_fact 抽 2..4），該格為 **OOD**，")
    print("      單獨標示、不作 gate 判準。")
    json.dump(res, open(os.path.join(HERE, "results_bridge_latent_gate.json"), "w"),
              indent=2, ensure_ascii=False)
    print("  -> results_bridge_latent_gate.json")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 250)
