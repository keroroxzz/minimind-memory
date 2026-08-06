"""新 core 的三個 gate —— 在投入任何 delivery 研究之前必須先過。

§4.50 鎖死。這三關全部是**關於 core 的**，不是關於記憶的：它們的作用是
**排除「core 看不懂語法／深度不夠」被誤判成 memory FAIL** —— G4b 就是這樣
把一個格式問題看成「temporal binding 學不起來」。

    gate 1  `L0` 顯式答案天花板與 loop ceiling
            → **`num_loops` 只看 `L0` 選**，不沿用 S₅ 的 2
    gate 2  oracle carrier 在 `j=0` 的 **consumer ceiling**
    gate 3  oracle carrier 在 `j=1`（最多 `j=2`）的 **fusion ceiling**

「oracle carrier」= **oracle inline**（把 placeholder 的 embedding 換成真值
token 的 embedding），**零參數** —— 與 core 訓練時用的是同一個機制。
所以 gate 2/3 檢查的是**core 對 carrier 格式的流利度**，
**不是**「學到的 delivery 有沒有用」。後者要等 adapter，是下一個實驗。

**任何 `L0` < 95% 的格子 censor** —— 那一格的 carrier 數字不得用來裁決記憶。
"""
import argparse
import json
import os
import random
import sys

import torch

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import bridge_renderer as B
from bridge_train_core import ARCH, DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))


@torch.no_grad()
def greedy(m, tok, ids, loops, embeds=None, max_new=4):
    out = m(ids.unsqueeze(0).to(DEVICE),
            inputs_embeds=None if embeds is None else embeds.unsqueeze(0).to(DEVICE),
            use_cache=True, num_loops=loops)
    pkv, got = out.past_key_values, []
    for _ in range(max_new):
        nxt = out.logits[:, -1].argmax(-1, keepdim=True)
        if nxt.item() == tok.eos_token_id:
            break
        got.append(nxt.item())
        out = m(nxt, past_key_values=pkv, use_cache=True, num_loops=loops)
        pkv = out.past_key_values
    return tok.decode(got, skip_special_tokens=True).strip()


@torch.no_grad()
def run_cell(m, tok, rng, j, loops, n, carrier):
    """carrier=False → L0；True → oracle inline（零參數，與訓練時同機制）。"""
    ok = 0
    for _ in range(n):
        ep = B.make_episode(rng, j)
        if not carrier:
            ids = torch.tensor(tok(tok.bos_token + B.render(ep)[0],
                                   add_special_tokens=False).input_ids)
            got = greedy(m, tok, ids, loops)
        else:
            mask = B.random_mask(rng, ep, p=0.6, force_used=True)
            ids, pos = B.value_positions(tok, ep, mask)
            src = torch.tensor(tok(tok.bos_token + B.render(ep, None)[0],
                                   add_special_tokens=False).input_ids)
            e = m.model.embed_tokens(ids.to(DEVICE)).clone()
            e[pos.to(DEVICE)] = m.model.embed_tokens(src.to(DEVICE))[pos.to(DEVICE)]
            got = greedy(m, tok, ids, loops, embeds=e)
        ok += int(got == ep.answer)
    return ok / n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", default="bridge_core.pth")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--js", type=int, nargs="+", default=[0, 1, 2, 3, 4])
    ap.add_argument("--loops", type=int, nargs="+", default=[1, 2, 3, 4])
    ap.add_argument("--seed", type=int, default=606060)
    a = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    blob = torch.load(os.path.join(HERE, a.ckpt), map_location="cpu")
    cfg = MiniMindConfig(vocab_size=blob["vocab"], **blob["arch"])
    m = MiniMindForCausalLM(cfg).to(DEVICE).eval()
    m.load_state_dict(blob["model"])
    print(f"  {a.ckpt}  step={blob['step']}  budget num_loops={blob['arch']['num_loops']}\n")

    # ---- gate 1：L0 天花板與 loop ceiling（**只看 L0**）----
    print(f"  ---- gate 1: L0 depth sweep（num_loops 只由這一關決定）")
    print(f"  {'loops':>6s} " + " ".join(f"{'j='+str(j):>8s}" for j in a.js))
    g1 = {}
    for L in a.loops:
        rng = random.Random(a.seed)
        row = [run_cell(m, tok, rng, j, L, a.n, False) for j in a.js]
        g1[L] = row
        print(f"  {L:>6d} " + " ".join(f"{x:8.1%}" for x in row))

    # 預先登記的選法：取「達到 L0>=95% 的最大 j」最大者；平手取較小的 loops
    def ceiling(row):
        c = -1
        for j, x in zip(a.js, row):
            if x >= 0.95:
                c = j
        return c
    best = max(a.loops, key=lambda L: (ceiling(g1[L]), -L))
    print(f"\n  各 loops 的 L0 ceiling（最大仍 >=95% 的 j）："
          + "  ".join(f"loops{L}:{ceiling(g1[L])}" for L in a.loops))
    print(f"  **選定 num_loops = {best}**（預先登記規則：ceiling 最大、平手取小）")

    # ---- gate 2/3：oracle carrier 的 consumer / fusion ceiling ----
    print(f"\n  ---- gate 2/3: oracle carrier（oracle inline，零參數）@ num_loops={best}")
    print(f"  {'j':>3s} {'L0':>8s} {'carrier':>9s} {'gap':>8s}   判定")
    g23, verdict = {}, []
    for j in a.js:
        rng = random.Random(a.seed + 11)
        l0 = run_cell(m, tok, rng, j, best, a.n, False)
        rng = random.Random(a.seed + 11)
        ca = run_cell(m, tok, rng, j, best, a.n, True)
        g23[j] = {"L0": l0, "carrier": ca}
        if l0 < 0.95:
            tag = "**censored**（L0 < 95%，此格不得裁決記憶）"
        elif (l0 - ca) > 0.05:
            tag = "core 對 carrier 格式不夠流利"; verdict.append(j)
        else:
            tag = "通過"
        print(f"  {j:>3d} {l0:>8.1%} {ca:>9.1%} {(l0-ca)*100:>+7.1f}pp   {tag}")

    g2 = g23.get(0, {})
    g3 = g23.get(1, {})
    ok2 = g2.get("L0", 0) >= 0.95 and (g2["L0"] - g2["carrier"]) <= 0.05
    ok3 = g3.get("L0", 0) >= 0.95 and (g3["L0"] - g3["carrier"]) <= 0.05
    print(f"\n  gate 2（j=0 consumer ceiling）{'✅' if ok2 else '❌'}   "
          f"gate 3（j=1 fusion ceiling）{'✅' if ok3 else '❌'}")
    print(f"\n  ⚠️ 這三關是**關於 core** 的，不是關於記憶的。它們過了只代表"
          f"\n     『core 看得懂語法、深度夠』，可以開始投入 delivery 研究；"
          f"\n     它們**不**代表學到的 delivery 會有用 —— 那是下一個實驗。")
    json.dump({"ckpt": a.ckpt, "step": blob["step"], "n": a.n, "seed": a.seed,
               "gate1_L0_sweep": {str(L): g1[L] for L in a.loops},
               "chosen_num_loops": best, "gate23": {str(j): g23[j] for j in g23},
               "gate2_pass": ok2, "gate3_pass": ok3},
              open(os.path.join(HERE, "results_bridge_gates.json"), "w"),
              indent=2, ensure_ascii=False)
    print(f"  -> results_bridge_gates.json")


if __name__ == "__main__":
    main()
