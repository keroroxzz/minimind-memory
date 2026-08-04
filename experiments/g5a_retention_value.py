"""G5a：**記憶的價值主張（第一題）—— 資訊保持 / context extension**。

⚠️ 這一題**不冒充「推理變強」**。§4.36 已經量到 k=8 時 executor 在 chance，
   深度上限是 core 的事，記憶動不了。這裡問的是**另一件事**：

    definition 事件**離開 active context** 之後，
    記憶能不能讓同一個 core 在**延遲之後**仍然答對？

### 五個配對條件（同 core、同 query、同 IDs、同題目）

| 條件 | prompt 裡有什麼 | 交付 |
|---|---|---|
| `oracle_L0` | 值**直接寫在 carrier 位置**，無前綴 | — |
| `full_context` | 值寫在 carrier + **definition 前綴 + d 個 filler** | — |
| `memory` | placeholder，**無前綴** | 走完整閉環（writer→store→guard→zdelta）|
| `shuffled` | placeholder，無前綴 | **別條 entry** 的 latent |
| `no_memory` | placeholder，無前綴 | 零向量 |

`full_context` 是「**什麼都留在 context 裡**」的替代方案 —— 記憶要贏的就是它。
`oracle_L0` 是**與延遲無關的絕對天花板**。

### 事前鎖死的判讀

- primary 看**準確率隨 delay 的變化**：`memory` 必須顯著贏
  `no_memory` 與 `shuffled`，且距 `oracle_L0` 天花板 **≤ 5pp**。
- 另報 **prompt token 數**（成本）與 delay / k 分層。
- 通過**只能**宣稱「**擴展了可用資訊與延遲依賴**」，
  **不能**宣稱提高了 executor 的 reasoning depth。
"""
import argparse
import json
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
from g1_oracle_inline import value_positions
from g1_teacher_kv import greedy_override
from g1_train import ARCH, DEVICE
from g2c_cal_v2 import wilson
from g3a_train import precompute_events
from model.memory_module import (ADDR_DIM, LATENT_DIM, LatentStore, MemoryEntry,
                                 perm_to_latent)

HERE = os.path.dirname(os.path.abspath(__file__))
CONDS = ("oracle_L0", "full_context", "memory", "shuffled", "no_memory")


def prefix_for(s, delay):
    """`full_context` 的前綴：definition 事件 + delay 個 filler。"""
    defs = " ".join(R.render_define_view(g, s.defs[g]) for g in dict.fromkeys(s.chain))
    return (defs + " " + " ".join([R.FILLER] * delay)).strip()


@torch.no_grad()
def run(m, tok, dl, writer, ev, s, cond, delay, other_perm):
    if cond in ("oracle_L0", "full_context"):
        prompt, ans = R.render_L0(s)
        if cond == "full_context":
            prompt = prefix_for(s, delay) + " " + prompt
        ids = torch.tensor(tok(tok.bos_token + prompt,
                               add_special_tokens=False).input_ids)
        got = greedy_override(m, tok, ids, None, ARCH["num_loops"])
        return got == ans, len(ids)

    # ---- 三個 placeholder 條件：prompt **完全相同**，只有交付內容不同 ----
    _, b_ids, pos = value_positions(tok, s)
    if cond == "memory":
        store = LatentStore()
        for g in dict.fromkeys(s.chain):
            z = writer(ev[(g, tuple(s.defs[g]))].unsqueeze(0))[0]
            store.commit([MemoryEntry(g, z, {"key": g})])
        lat = []
        for g in s.chain:
            if g not in store._entries:          # membership guard（§4.38）
                return None, len(b_ids)
            lat.append(store.read([g])[0].latent.to(DEVICE))
        lat = torch.stack(lat).unsqueeze(0)
    elif cond == "shuffled":
        lat = torch.stack([perm_to_latent(other_perm).to(DEVICE)
                           for _ in s.chain]).unsqueeze(0)
    else:
        lat = torch.zeros(1, len(s.chain), LATENT_DIM, device=DEVICE)
    got = greedy_override(m, tok, b_ids, G3._mk_fn(dl, lat, pos.to(DEVICE)),
                          ARCH["num_loops"])
    return got == R.render_L0(s)[1], len(b_ids)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--ks", type=int, nargs="+", default=[1, 2, 4])
    ap.add_argument("--delays", type=int, nargs="+", default=[0, 8, 24, 64])
    ap.add_argument("--seed", type=int, default=31337)
    a = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    m, dl, writer, _, _, _ = G3D.load_all(tok, "g3a_writer_6820855741.pth",
                                          "results_g2b.json")
    addressable = [k for k in R.ALL_KEYS if int(k[1:]) < ADDR_DIM]
    two = G3B.two_token_keys(tok, addressable)
    _, perms = R.perm_splits()
    G3.WRITE_KEYS = sorted(set(two))
    ev = precompute_events(m, tok, perms)

    print(f"  **全凍結、零訓練**；同 core、同 query、同 IDs、同題目，五個條件配對")
    print(f"  `full_context` = 什麼都留在 context 裡（definition 前綴 + delay 個 filler）")
    print(f"  `oracle_L0` = 值直接寫在 carrier、**與延遲無關的絕對天花板**\n")

    rng = random.Random(a.seed)
    res = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    toks = defaultdict(lambda: defaultdict(list))
    for k in a.ks:
        # 置換必須來自 `perms`（事件快取就是用它建的），所以用受限的建構器
        base = [G3.make_sample(k, rng, perms) for _ in range(a.n)]
        for d in a.delays:
            for s in base:
                op = list(perms[rng.randrange(len(perms))])
                for c in CONDS:
                    ok, nt = run(m, tok, dl, writer, ev, s, c, d, op)
                    if ok is None:
                        continue
                    r = res[c][(k, d)]
                    r[1] += 1; r[0] += int(ok)
                    toks[c][(k, d)].append(nt)

    for k in a.ks:
        print(f"  ── k={k}")
        print(f"  {'條件':<14s} " + "  ".join(f"d={d:<3d}" for d in a.delays)
              + "     prompt tokens（d=最大）")
        for c in CONDS:
            cells = "  ".join(f"{res[c][(k,d)][0]/max(res[c][(k,d)][1],1):5.1%}"
                              for d in a.delays)
            tk = toks[c][(k, a.delays[-1])]
            print(f"  {c:<14s} {cells}     {sum(tk)/max(len(tk),1):6.1f}")
        print()

    # ---- primary gate ----
    def agg(c):
        n = sum(res[c][(k, d)][1] for k in a.ks for d in a.delays)
        v = sum(res[c][(k, d)][0] for k in a.ks for d in a.delays)
        return v / max(n, 1), n
    mem, n_mem = agg("memory"); ceil_, _ = agg("oracle_L0")
    nom, _ = agg("no_memory"); shf, _ = agg("shuffled"); fc, _ = agg("full_context")
    print(f"  **整體**  oracle_L0 {ceil_:.1%} | memory {mem:.1%} "
          f"[{wilson(int(mem*n_mem), n_mem)[0]:.1%}, {wilson(int(mem*n_mem), n_mem)[1]:.1%}] "
          f"| full_context {fc:.1%} | shuffled {shf:.1%} | no_memory {nom:.1%}")
    gap = ceil_ - mem
    ok_beat = mem > max(nom, shf) + 0.05
    ok_gap = gap <= 0.05
    print(f"\n  gate 1：memory 顯著贏 no_memory/shuffled  "
          f"{'✅' if ok_beat else '❌'}（{mem:.1%} vs {max(nom, shf):.1%}）")
    print(f"  gate 2：距 oracle_L0 天花板 ≤ 5pp        "
          f"{'✅' if ok_gap else '❌'}（差 {gap*100:.1f}pp）")
    print(f"\n  裁決：**{'PASS' if (ok_beat and ok_gap) else 'FAIL'}**")
    print(f"  ⚠️ 通過**只能**宣稱「**擴展了可用資訊與延遲依賴**」，"
          f"\n     **不能**宣稱提高了 executor 的 reasoning depth ——"
          f"\n     §4.36 已量到 k=8 時 core 在 chance，那個上限記憶動不了。")
    json.dump({"cells": {c: {f"k{k}_d{d}": res[c][(k, d)] for k in a.ks
                             for d in a.delays} for c in CONDS},
               "tokens": {c: {f"k{k}_d{d}": (sum(toks[c][(k, d)])
                                             / max(len(toks[c][(k, d)]), 1))
                              for k in a.ks for d in a.delays} for c in CONDS},
               "overall": {"oracle_L0": ceil_, "memory": mem, "full_context": fc,
                           "shuffled": shf, "no_memory": nom},
               "verdict": "PASS" if (ok_beat and ok_gap) else "FAIL"},
              open(os.path.join(HERE, "results_g5a.json"), "w"), indent=2,
              ensure_ascii=False)
    print(f"  -> results_g5a.json")


if __name__ == "__main__":
    main()
