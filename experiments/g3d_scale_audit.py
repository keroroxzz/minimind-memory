"""G3d：**boundary audit** —— 全凍結、不重校、不訓練，只問現有結論撐到哪裡。

Codex 的兩條規定：

  1. **兩個單變因，不可同時放大** store size 與 chain length。
  2. `k` 那條**先跑 oracle ceiling**；oracle 先塌就停，不必再測 learned。

    `--axis pool`   pool 8 → 16 → 32，**k 固定**
    `--axis k`      k 1 → 2 → 4 → 8 …，**pool 固定**

硬上限與它們的來源，讀數字前必須知道：

  `ADDR_DIM = 32`   `address_vector` 的正交 bank 只有 32 條 → **pool 最大 32**
  2-token key 29 個 → pool > 29 時，第 30 條起用 3-token key 當 distractor。
                     這**不影響結論**：distractor 的 address 是固定正交的、
                     與 token 結構無關，而它們的**內容永遠不會被讀到**
                     （query key 一律是 `f0..f3`）。
  core 是 `num_loops=2`（16 個序列步）→ 依 §「looped transformer」的表，
                     ceiling k* ≈ 4.83。所以 **k 這條預期在 k≈5 附近由 oracle 先塌**，
                     那是 **executor 的深度上限**，不是記憶系統的問題。

⚠️ 每個 scale **不重校 threshold、不訓練**，報**相同的因果鏈**與 R4 安全指標。
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
from g1_train import ARCH, BACKBONE, DEVICE, make_delivery
from g3a_train import CORE_ZD, Writer, precompute_events
from model.memory_module import ADDR_DIM, Retriever
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))


def load_all(tok, writer_ckpt, g2b_results):
    m = MiniMindForCausalLM(MiniMindConfig(**BACKBONE, **ARCH)).to(DEVICE).eval()
    blob = torch.load(os.path.join(HERE, CORE_ZD), map_location="cpu")
    m.load_state_dict(blob["model"], strict=False)
    dl = make_delivery("zdelta", BACKBONE["num_hidden_layers"], ARCH["num_loops"],
                       os.path.join(HERE, "g1_renderer_artifact.json"))
    dl.load_state_dict(blob["delivery"]); dl.eval()
    writer = Writer(BACKBONE["hidden_size"], out_param="rowsoftmax").to(DEVICE).eval()
    writer.load_state_dict(torch.load(os.path.join(HERE, writer_ckpt), map_location="cpu"))
    thr = json.load(open(os.path.join(HERE, g2b_results)))["threshold"]
    ck = sorted([f for f in os.listdir(HERE) if f.startswith("g2b_retriever_")],
                key=lambda f: os.path.getmtime(os.path.join(HERE, f)))[-1]
    ret = Retriever(BACKBONE["hidden_size"], ADDR_DIM).to(DEVICE).eval()
    sd = torch.load(os.path.join(HERE, ck), map_location="cpu")
    ret.load_state_dict(sd["retriever"] if "retriever" in sd else sd)
    for p in list(m.parameters()) + list(dl.parameters()) + \
            list(writer.parameters()) + list(ret.parameters()):
        p.requires_grad_(False)
    return m, dl, writer, ret, thr, ck


def evaluate(m, tok, dl, writer, ret, ev, eps, thr, use_writer, use_retrieval):
    f = [0, 0]; ad = [0, 0]; c = [0, 0]; e = [0, 0]
    r4 = defaultdict(int)
    for s, pool, pp, om in eps:
        r = G3B.run_episode(m, tok, dl, writer, ret, ev, s, pool, pp, om, thr,
                            use_writer, use_retrieval, True, None)
        for v in r["form_ok"]:
            if v is not None:
                f[1] += 1; f[0] += int(v)
        for v in r["addr_ok"]:
            ad[1] += 1; ad[0] += int(v)
        for v in r["content_ok"]:
            if v is not None:
                c[1] += 1; c[0] += int(v)
        if om is not None:
            r4["miss_n"] += 1
            r4["R_abstain"] += int(r["abstained"]); r4["halluc"] += int(not r["abstained"])
        else:
            r4["ans_n"] += 1; r4["false_abstain"] += int(r["abstained"])
            e[1] += 1; e[0] += int(bool(r["e2e"]))
    return {"formation": f, "address": ad, "content": c, "e2e": e, "r4": dict(r4)}


def show(tag, r):
    f, ad, c, e = r["formation"], r["address"], r["content"], r["e2e"]
    q = r["r4"]; mn, an = max(q.get("miss_n", 0), 1), max(q.get("ans_n", 0), 1)
    print(f"  {tag:<12s} {f[0]/max(f[1],1):9.1%} {ad[0]/max(ad[1],1):10.1%} "
          f"{c[0]/max(c[1],1):9.1%} {e[0]/max(e[1],1):9.1%} "
          f"{q.get('R_abstain',0)/mn:10.1%} {q.get('halluc',0)/mn:8.1%} "
          f"{q.get('false_abstain',0)/an:11.1%}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--axis", required=True, choices=["pool", "k"])
    ap.add_argument("--pools", type=int, nargs="+", default=[8, 16, 32])
    ap.add_argument("--ks", type=int, nargs="+", default=[1, 2, 4, 8])
    ap.add_argument("--fixed-k", type=int, default=4)
    ap.add_argument("--fixed-pool", type=int, default=8)
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--p-omit", type=float, default=0.15)
    ap.add_argument("--writer-ckpt", default="g3a_writer_6820855741.pth")
    ap.add_argument("--g2b-results", default="results_g2b.json")
    ap.add_argument("--seed", type=int, default=4321)
    ap.add_argument("--oracle-floor", type=float, default=0.5,
                    help="oracle end-to-end 低於此值就停止該軸（不再測 learned）")
    a = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    m, dl, writer, ret, thr, ck = load_all(tok, a.writer_ckpt, a.g2b_results)

    addressable = [k for k in R.ALL_KEYS if int(k[1:]) < ADDR_DIM]
    two = G3B.two_token_keys(tok, addressable)
    query_keys = [k for k in R.KEYS if k in two]
    # distractor：先用 2-token，不夠再補 3-token（它們的內容永不被讀到）
    distractors = two + [k for k in addressable if k not in two]
    _, perms = R.perm_splits()
    G3.WRITE_KEYS = sorted(set(query_keys) | set(distractors))
    ev = precompute_events(m, tok, perms)

    print(f"  **全凍結、不重校、不訓練**（threshold {thr:+.3f} 沿 G2b）")
    print(f"  query key {query_keys}；可定址身分 {len(addressable)}"
          f"（其中 2-token {len(two)}）；ADDR_DIM={ADDR_DIM} 是 pool 的硬上限")
    print(f"  軸 = {a.axis}；另一維固定："
          f"{'k=' + str(a.fixed_k) if a.axis == 'pool' else 'pool=' + str(a.fixed_pool)}\n")

    hdr = (f"  {'設定':<12s} {'formation':>9s} {'retrieval':>10s} {'content':>9s} "
           f"{'e2e':>9s} {'R_abstain':>10s} {'halluc':>8s} {'false_abst':>11s}")
    out = {}
    values = a.pools if a.axis == "pool" else a.ks
    for v in values:
        pool = v if a.axis == "pool" else a.fixed_pool
        k = a.fixed_k if a.axis == "pool" else v
        if pool > len(distractors) + len(query_keys) or pool > ADDR_DIM:
            print(f"  pool={pool} 超過可定址上限，跳過"); continue
        rng = random.Random(a.seed)
        eps = [G3B.make_episode(k, rng, query_keys, distractors, perms, pool, a.p_omit)
               for _ in range(a.n)]
        print(f"  ── {a.axis}={v}（{len(eps)} episodes，pool={pool}, k={k}）")
        print(hdr)
        # **oracle 先跑**：它塌了就沒必要再測 learned（Codex）
        o = evaluate(m, tok, dl, writer, ret, ev, eps, thr, False, False)
        show("oracle", o)
        o_e2e = o["e2e"][0] / max(o["e2e"][1], 1)
        out[f"{a.axis}={v}"] = {"oracle": o}
        if o_e2e < a.oracle_floor:
            print(f"  → **oracle e2e {o_e2e:.1%} < {a.oracle_floor:.0%}，"
                  f"executor 天花板已塌，停止此軸**（不再測 learned）\n")
            break
        l = evaluate(m, tok, dl, writer, ret, ev, eps, thr, True, True)
        show("learned", l)
        out[f"{a.axis}={v}"]["learned"] = l
        print()

    json.dump({"axis": a.axis, "threshold": thr, "retriever": ck,
               "query_keys": query_keys, "n": a.n, "seed": a.seed, "cells": out},
              open(os.path.join(HERE, f"results_g3d_{a.axis}.json"), "w"),
              indent=2, ensure_ascii=False)
    print(f"  ⚠️ 這是 **boundary audit**：任何下降都要先問是 executor 天花板"
          f"還是記憶系統，\n     oracle 那一列就是用來分開這兩者的。")
    print(f"  -> results_g3d_{a.axis}.json")


if __name__ == "__main__":
    main()
