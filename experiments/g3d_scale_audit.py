"""G3d：**boundary audit** —— 全凍結、不重校、不訓練，只問現有結論撐到哪裡。

Codex 的兩條規定：

  1. **兩個單變因，不可同時放大** store size 與 chain length。
  2. `k` 那條**先跑 oracle ceiling**；oracle 先塌就停，不必再測 learned。

    `--axis pool`   pool 8 → 16 → 31，**k 固定**
    `--axis k`      k 1 → 2 → 4 → 8 …，**pool 固定**

硬上限與它們的來源，讀數字前必須知道：

  `ADDR_DIM = 32`   `address_vector` 的正交 bank 只有 32 條 → 可定址身分 32 個。
                     但 **pool 的上限是 31 不是 32**：漏寫題需要
                     「pool 裡 31 條 + 被漏掉的 1 條」，被漏掉的那條**不得**在 pool 裡
                     （否則 bank 會留下懸空 address，見 §4.34）。
                     同一個「至少要 POOL_SIZE+1」的約束，§4.29 在 G2c 的 key split
                     上已經踩過一次。
  2-token key 29 個 → pool > 29 時，第 30 條起用 3-token key 當 distractor。
                     它們**一律用 oracle 內容 commit、不經 learned writer**，
                     否則 writer 的 span OOD 會透過 formation/guard 改變 pool
                     membership，把 span 效應混進 pool-size 這一軸（Codex）。
                     address 仍是固定正交的，且腳本會回報
                     **distractor 從未被選中**，以證明其內容根本沒被讀到。
  `k* ≈ 4.83`       是 looped transformer 那張表給的**外部先驗**，
                     **不是本 audit 的既定原因**。k 軸的歸因一律由
                     `L0` 與 `oracle+zdelta` 兩列共同決定（見 `evaluate_L0`）。

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


@torch.no_grad()
def evaluate_L0(m, tok, eps):
    """**explicit-value L0**：值直接寫在 prompt 裡，**完全不經任何交付**。

    這是 k 軸歸因的關鍵對照（Codex）：`oracle latent → zdelta` 那列**仍然經過
    `zdelta`，而 zdelta 只在 k≤4 訓練過**，所以它塌掉可能是**交付外推失敗**
    而不是 executor 天花板。

      L0 先塌            → 支持 **core/executor ceiling**
      L0 穩、zdelta 塌   → **memory delivery 外推失敗**
      兩者同塌           → 只能說是**共同邊界**，不可歸因單一元件

    先前的 `k* = 4.83` 是**外部先驗**，不是本 audit 的既定原因。
    """
    from g1_teacher_kv import greedy_override
    ok = [0, 0]
    for s_, _, _, om in eps:
        if om is not None:
            continue                      # L0 沒有記憶可缺，漏寫題不適用
        prompt, ans = R.render_L0(s_)
        ids = torch.tensor(tok(tok.bos_token + prompt, add_special_tokens=False).input_ids)
        got = greedy_override(m, tok, ids, None, ARCH["num_loops"])
        ok[1] += 1; ok[0] += int(got == ans)
    return ok


def evaluate(m, tok, dl, writer, ret, ev, eps, thr, use_writer, use_retrieval):
    f = [0, 0]; ad = [0, 0]; c = [0, 0]; e = [0, 0]
    r4 = defaultdict(int)
    picked_distractor = 0
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
        # distractor 若從未被選中，它們的**內容**就與結論無關 ——
        # 這正是「3-token distractor 用 oracle 內容」不污染 pool 軸的直接證據。
        req = set(s.chain)
        picked_distractor += sum(1 for x in r.get("picked", []) if x not in req)
    return {"formation": f, "address": ad, "content": c, "e2e": e, "r4": dict(r4),
            "picked_distractor": picked_distractor}


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
    # ⚠️ 最大是 **31 不是 32**：ADDR_DIM=32 給出 32 個可定址身分，但漏寫題需要
    #    「pool 裡 32 條 + 被漏掉的 1 條」= 33 個。所以 pool 的上限是
    #    **可定址身分數 − 1**。（同一個「split 至少要 POOL_SIZE+1」的約束，
    #    §4.29 已經在 G2c 的 key split 上踩過一次。）
    ap.add_argument("--pools", type=int, nargs="+", default=[8, 16, 31])
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
    # distractor：先用 2-token，不夠再補 3-token。
    # ⚠️ **3-token distractor 一律用 oracle 內容 commit，不經 learned writer**（Codex）：
    #    否則 writer 的 span OOD 會透過 formation/guard 改變 pool membership，
    #    把 span 效應混進 pool-size 這一軸。它們的 address 仍是固定正交的，
    #    而且下面會回報「distractor 是否曾被選中」以證明內容根本沒被讀到。
    three = [k for k in addressable if k not in two]
    distractors = two + three
    G3B.ORACLE_CONTENT_KEYS = set(three)
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
        n_ident = len(set(distractors) | set(query_keys))
        if pool > n_ident - 1:
            print(f"  pool={pool} 超過上限（可定址身分 {n_ident}，漏寫題需留 1 條 → "
                  f"最大 {n_ident - 1}），跳過\n"); continue
        rng = random.Random(a.seed)
        eps = [G3B.make_episode(k, rng, query_keys, distractors, perms, pool, a.p_omit)
               for _ in range(a.n)]
        print(f"  ── {a.axis}={v}（{len(eps)} episodes，pool={pool}, k={k}）")
        print(hdr)
        # **雙 oracle**：先跑完全不經交付的 L0，才分得開 executor 與 delivery
        l0 = evaluate_L0(m, tok, eps)
        print(f"  {'L0（無交付）':<12s} {'--':>9s} {'--':>10s} {'--':>9s} "
              f"{l0[0]/max(l0[1],1):9.1%} {'--':>10s} {'--':>8s} {'--':>11s}")
        o = evaluate(m, tok, dl, writer, ret, ev, eps, thr, False, False)
        show("oracle+zdelta", o)
        if o.get("picked_distractor"):
            print(f"       ⚠️ distractor 被選中 {o['picked_distractor']} 次 —— "
                  f"它們的內容不再與結論無關")
        o_e2e = o["e2e"][0] / max(o["e2e"][1], 1)
        out[f"{a.axis}={v}"] = {"L0": l0, "oracle": o}
        l0_e2e = l0[0] / max(l0[1], 1)
        if o_e2e < a.oracle_floor:
            who = ("**L0 也塌了（{:.1%}）→ 共同邊界，不可單獨歸因**".format(l0_e2e)
                   if l0_e2e < a.oracle_floor
                   else "**L0 仍有 {:.1%} → 是 delivery 外推失敗，不是 executor**"
                        .format(l0_e2e))
            print(f"  → oracle+zdelta e2e {o_e2e:.1%} < {a.oracle_floor:.0%}，停止此軸。"
                  f"\n     {who}\n")
            break
        l = evaluate(m, tok, dl, writer, ret, ev, eps, thr, True, True)
        show("learned", l)
        print(f"       distractor 被選中：oracle {o['picked_distractor']} 次 / "
              f"learned {l['picked_distractor']} 次"
              f"{'（皆為 0 → 其內容與結論無關）' if not (o['picked_distractor'] or l['picked_distractor']) else ''}")
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
