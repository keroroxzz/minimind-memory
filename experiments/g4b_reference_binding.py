"""G4b：**closed-world temporal / reference binding** —— 最小版本。

事件是一段**有序的 stream**：若干次 literal 寫入 + 若干個填充事件。
query **用指稱**（`前者` = 最後被寫入的 entity）而非 literal key。

    | f4 = … 定 | f18 = … 定 | f29 = … 定 | 略 | 略
    | x=STATE | . . . . . 求x=（前者）        ← 指涉 f29

除了新增的 **reference resolver**，**writer / zdelta / store / retrieval 全部凍結**，
每個 episode 嚴格 reset。

### 為什麼指稱放在**讀取端**

§4.38 的 exact-membership guard 只能擋**不存在**的 key。
它**擋不住 resolver 錯指到另一個已存在的 entity**（Codex）——
那個 key 確實在 store 裡，guard 會放行，於是**自信地交付錯誤內容**。

所以 **`wrong-existing-entity` 是這一關唯一的危險錯誤**，
必須與 abstain **分開報**。在它可校準之前，
**只稱 closed-world temporal binding，不稱完整安全閉環**。
§4.30 的 G2c open-set blocker 仍原樣掛著。

### 階梯（一階一階來，不一口氣做完整自然語言）

    oracle resolver     → ceiling
    learned（small overfit） → 確認可學
    formal（固定 split） → 正式

同一批 episodes 做 oracle / learned 的介入對照。
分層變數：**距離**（填充事件數）與 **distractor entity 數**。
"""
import argparse
import hashlib
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
from g1_train import ARCH, BACKBONE, DEVICE
from g2c_cal_v2 import wilson
from g3a_train import precompute_events
from model.memory_module import (ADDR_DIM, LATENT_DIM, PERM_N, LatentStore, MemoryEntry,
                                 address_vector)

HERE = os.path.dirname(os.path.abspath(__file__))


# 交付用的 prompt 直接沿用 §4.23 起就沒動過的 `value_positions`，
# **不加任何前綴後綴** —— stream 與指稱走 out-of-band 的 reference view。


@torch.no_grad()
def resolve_oracle(target, writes):
    return target


@torch.no_grad()
def run_episode(m, tok, dl, writer, ev, stream, writes, target, s, resolver, stats):
    """全凍結。resolver 決定指稱指向哪個 entity；其餘照 §4.38 的契約走。"""
    store = LatentStore()
    for kx, p in writes:                       # stream 的每次寫入都真的進 store
        z = writer(ev[(kx, tuple(p))].unsqueeze(0))[0]
        store.commit([MemoryEntry(kx, z, {"key": kx})])

    picked = resolver(target, writes)
    stats["ref_n"] += 1
    stats["ref_ok"] += int(picked == target)

    # ---- exact-membership guard（§4.38）：只擋不存在的 key ----
    if picked is None or picked not in store._entries:
        stats["abstain"] += 1
        return dict(abstained=True, e2e=None, wrong_entity=False)
    e = store.read([picked])[0]
    if e is None or e.address != picked or e.latent.shape != (LATENT_DIM,):
        stats["abstain"] += 1
        return dict(abstained=True, e2e=None, wrong_entity=False)

    # ⚠️ 指錯到**另一個已存在的 entity** —— guard 放行，這就是危險錯誤
    wrong = (picked != target)
    stats["wrong_entity"] += int(wrong)

    _, b_ids, pos = value_positions(tok, s)          # **凍結 core 訓練過的格式，不動**
    lat = e.latent.to(DEVICE).unsqueeze(0).expand(s.k, -1).unsqueeze(0)
    got = greedy_override(m, tok, b_ids, G3._mk_fn(dl, lat, pos.to(DEVICE)),
                          ARCH["num_loops"])
    return dict(abstained=False, e2e=(got == R.render_L0(s)[1]), wrong_entity=wrong)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="oracle", choices=["oracle", "learned"])
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--k", type=int, default=1)
    ap.add_argument("--entities", type=int, nargs="+", default=[2, 3, 5])
    ap.add_argument("--fillers", type=int, nargs="+", default=[0, 2, 5])
    ap.add_argument("--seed", type=int, default=606)
    ap.add_argument("--writer-ckpt", default="g3a_writer_6820855741.pth")
    ap.add_argument("--g2b-results", default="results_g2b.json")
    a = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    m, dl, writer, _, _, _ = G3D.load_all(tok, a.writer_ckpt, a.g2b_results)
    addressable = [k for k in R.ALL_KEYS if int(k[1:]) < ADDR_DIM]
    two = G3B.two_token_keys(tok, addressable)
    _, perms = R.perm_splits()
    G3.WRITE_KEYS = sorted(set(two))
    ev = precompute_events(m, tok, perms)

    resolver = {"oracle": resolve_oracle}[a.stage]
    print(f"  **全凍結、零訓練**；resolver = **{a.stage}**")
    print(f"  writer/zdelta/store 全部沿用既有凍結權重；每 episode 嚴格 reset store")
    print(f"  指稱在**讀取端** → 錯指到已存在的 entity 會被 guard 放行 = **危險錯誤**\n")

    rng = random.Random(a.seed)
    print(f"  {'entities':>8s} {'fillers':>8s} {'ref exact':>10s} {'E2E':>8s} "
          f"{'**wrong-entity**':>16s} {'abstain':>8s}")
    out = {}
    for ne in a.entities:
        for nf in a.fillers:
            st = defaultdict(int)
            e2e = [0, 0]
            for _ in range(a.n):
                stream, writes, target, s = R.make_reference_episode(
                    rng, two, perms, ne, nf, a.k)
                r = run_episode(m, tok, dl, writer, ev, stream, writes, target, s,
                                resolver, st)
                if not r["abstained"]:
                    e2e[1] += 1; e2e[0] += int(bool(r["e2e"]))
            key = f"e{ne}_f{nf}"
            out[key] = {**dict(st), "e2e": e2e}
            print(f"  {ne:>8d} {nf:>8d} {st['ref_ok']/max(st['ref_n'],1):9.1%} "
                  f"{e2e[0]/max(e2e[1],1):7.1%} "
                  f"{st['wrong_entity']/max(st['ref_n'],1):15.1%} "
                  f"{st['abstain']/max(st['ref_n'],1):7.1%}")

    tot_ref = sum(v["ref_ok"] for v in out.values())
    tot_n = sum(v["ref_n"] for v in out.values())
    tot_w = sum(v["wrong_entity"] for v in out.values())
    tot_e = sum(v["e2e"][0] for v in out.values())
    tot_en = sum(v["e2e"][1] for v in out.values())
    print(f"\n  整體 ref exact {tot_ref/tot_n:.1%}（n={tot_n}）   "
          f"E2E {tot_e/max(tot_en,1):.1%} [{wilson(tot_e, tot_en)[0]:.1%}, "
          f"{wilson(tot_e, tot_en)[1]:.1%}]")
    print(f"  **wrong-existing-entity {tot_w}/{tot_n} = {tot_w/tot_n:.1%}** "
          f"← 唯一的危險錯誤，**與 abstain 分開報**")
    print(f"\n  ⚠️ 這一階是 **{a.stage} resolver 的天花板**。"
          f"\n     exact-membership guard **擋不住**指錯到已存在的 entity ——"
          f"\n     在 `wrong-existing-entity` 可校準之前，"
          f"**只稱 closed-world temporal binding，不稱完整安全閉環**。"
          f"\n     §4.30 的 G2c open-set blocker 仍原樣掛著。")
    chk = hashlib.sha256(json.dumps(out, sort_keys=True).encode()).hexdigest()[:12]
    json.dump({"stage": a.stage, "k": a.k, "n_per_cell": a.n, "seed": a.seed,
               "cells": out, "checksum": chk},
              open(os.path.join(HERE, f"results_g4b_{a.stage}.json"), "w"),
              indent=2, ensure_ascii=False)
    print(f"  -> results_g4b_{a.stage}.json")


if __name__ == "__main__":
    main()
