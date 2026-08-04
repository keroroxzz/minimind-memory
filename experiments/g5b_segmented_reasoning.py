"""G5b：**記憶能不能繞過「整體鏈長」**（第二題，越級打怪真正該有的測法）。

§4.36 量到單次 core 在 k≥5 就崩，k=8 在 chance。這一題問的是**另一件事**：
把長度 K 的 chain **拆成每段 ≤4**，中間結果 checkpoint 出去再拿回來，
**多次 core 呼叫**能不能達成單次做不到的整體鏈長？

### 代數（先確認才不會測到假東西）

本專案的更新是 `st ← st ∘ p`。所以**從 identity 出發**跑 p1..pj 得到的就是
**合成置換** `P_j = p1∘…∘pj`，而 `st0 ∘ P_j = st_j`。
於是 checkpoint 可以是**一個置換**，**放進 value slot** ——
那正是 `zdelta` 唯一訓練過的交付角色，不必去碰狀態位置。

    seg1   x=identity, [p1 p2 p3 p4]        → P4
    seg2   x=identity, [P4 p5 p6 p7]        → P7
    seg3   x=identity, [P7 p8 …]            → …
    final  x=st0,      [P_all]              → 答案

**每一次呼叫都 k≤4，單次 core 深度完全沒有被突破。**

### 四個條件（Codex：三組不夠）

| | 條件 | 隔離什麼 |
|---|---|---|
| ① | `monolithic` 不拆段，k=K 一次做完 | 基準（已知會崩）|
| ② | `oracle_ckpt` 拆段 + **真值** checkpoint（文字）| **天花板** |
| ③ | `pred_text` 拆段 + **模型預測**的 checkpoint（文字）| ②vs③ = 中間誤差傳播 |
| ④ | `pred_memory` 同一預測經 **commit→store→retrieve→zdelta 交付** | ③vs④ = 記憶序列化／往返 |

①vs③/④ 才是**分段計算增益**。

### 事前鎖死的措辭

通過只能說「**多次 core 呼叫 + checkpoint 繞過了整體鏈長**」，
**不能**說單次 executor 變深。monolithic 與 segmented 的**計算量不同** ——
這是**系統能力／compute tradeoff**，**不是**等算力的模型能力比較。

⚠️ 沿用同一個 S₅ latent schema 是**合法且應該先用**的：S₅ 對 composition 封閉，
   這是明確的 **homogeneous closed-type checkpointing 正控制**，不是作弊。
   但限制必寫：**結果可能依賴代數閉包與型別同構**；
   異質中間狀態另立後續的泛化題。
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
from g1_teacher_kv import greedy_override
from g1_train import ARCH, DEVICE
from g2c_cal_v2 import wilson
from g3a_train import precompute_events
from model.memory_module import (ADDR_DIM, LatentStore, MemoryEntry, PERM_N,
                                 latent_to_perm, perm_to_latent)

HERE = os.path.dirname(os.path.abspath(__file__))
IDENT = list(range(PERM_N))
CONDS = ("monolithic", "oracle_ckpt", "pred_text", "pred_memory")

# ---- post-smoke **exploratory diagnostic**（Codex）：不改動上面四組 primary ----
#   ⑤ `pred_mem_allph`   該段**所有 slot 都 placeholder、全部走交付**
#   ⑥ `oracle_mem_mixed` **真值** checkpoint 以 latent 交付 + 3 個明確 p
#
# 判讀事前鎖死：
#   ⑥ 也低、⑤ 高          → 支持 **mixed-carrier** 歸因
#   ⑥ 高、④ 低            → 斷點在 **checkpoint encoding／往返**
#   ⑤ 也低                → 假說被推翻或不足，屬更廣的 zdelta checkpoint 不轉移
# ⑤ 必須對照**同配置**的 oracle ceiling，**不先驗要求它一定 100%**。
DIAG = ("pred_mem_allph", "oracle_mem_allph", "oracle_mem_mixed")


def compose(a, b):
    """`a ∘ b`，與本專案的 `st ← [st[p[i]]]` 更新一致。"""
    return [a[b[i]] for i in range(PERM_N)]


def render_slots(state, slots):
    """value slot 可以是**明確的置換**或 `None`（= placeholder，留給交付）。"""
    body = " ".join(f"| {R.PLACEHOLDER}" if p is None else f"| {R._nums(p)}"
                    for p in slots)
    return f"| x={R._nums(state)} {body} 求x="


def slot_positions(tok, state, slots):
    """placeholder 那些 slot 的 token 位置（用『換成數字再比對』求得，不靠 regex）。"""
    a = tok(tok.bos_token + render_slots(state, slots),
            add_special_tokens=False).input_ids
    probe = [IDENT if p is None else p for p in slots]
    b = tok(tok.bos_token + render_slots(state, probe),
            add_special_tokens=False).input_ids
    assert len(a) == len(b), "placeholder 與數值 span 長度不一致"
    pos = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    assert len(pos) == sum(p is None for p in slots) * PERM_N
    return torch.tensor(a), torch.tensor(pos)


@torch.no_grad()
def call_core(m, tok, state, slots, dl=None, lat=None):
    """一次 core 呼叫。`lat` 非 None 時，placeholder slot 由 `zdelta` 交付。"""
    ids, pos = slot_positions(tok, state, slots)
    fn = None if lat is None else G3._mk_fn(dl, lat.unsqueeze(0), pos.to(DEVICE))
    out = greedy_override(m, tok, ids, fn, ARCH["num_loops"])
    try:
        p = [int(x) for x in out.split()]
        return p if sorted(p) == IDENT else None
    except ValueError:
        return None


def segments(K, seg_max=4):
    """回傳每段要新做幾步：第一段 seg_max 步，之後每段留 1 格給 checkpoint。"""
    out, done = [], 0
    while done < K:
        n = seg_max if done == 0 else seg_max - 1
        n = min(n, K - done)
        out.append(n); done += n
    return out


@torch.no_grad()
def run_segmented(m, tok, dl, writer, ev, chain, st0, mode, key, stats):
    """② / ③ / ④：拆段跑完，回傳最終答案（或 None）。

    `true_carry` 是**真值**的累積合成，`pred_carry` 是**模型輸出**的累積合成。
    `oracle_ckpt` 用前者、`pred_*` 用後者 —— 差別只在餵進下一段的是誰。
    """
    true_carry = pred_carry = true_carry_prev = None
    idx = 0
    for si, n in enumerate(segments(len(chain))):
        ps = chain[idx:idx + n]; idx += n
        seg = ps[0]
        for p in ps[1:]:
            seg = compose(seg, p)                      # 這一段本身的合成（真值）
        true_carry = seg if si == 0 else compose(true_carry, seg)

        if si == 0:
            slots, lat = list(ps), None
        else:
            use = (true_carry_prev
                   if mode in ("oracle_ckpt", "oracle_mem_mixed", "oracle_mem_allph")
                   else pred_carry)
            if use is None:
                return None
            if mode in ("pred_memory", "oracle_mem_mixed"):
                # **commit → store → retrieve → 交付**：checkpoint 走完整往返
                store = LatentStore()
                store.commit([MemoryEntry(key, perm_to_latent(use).to(DEVICE),
                                          {"key": key})])
                e = store.read([key])[0]
                if e is None or e.address != key:      # §4.38 的 guard
                    stats["guard"] += 1; return None
                slots, lat = [None] + list(ps), e.latent.to(DEVICE).unsqueeze(0)
            elif mode in ("pred_mem_allph", "oracle_mem_allph"):
                # ⑤：**整段都走交付** —— checkpoint 與該段的 p 全部 commit 再取回。
                #    該段的 p 用 **oracle/確定性 latent**（`perm_to_latent`），
                #    不引入新的 writer 誤差；roundtrip 由 guard 逐條驗。
                store = LatentStore()
                items = [use] + list(ps)
                for j, pp_ in enumerate(items):
                    store.commit([MemoryEntry(f"{key}#{j}",
                                              perm_to_latent(pp_).to(DEVICE), {})])
                lats = []
                for j in range(len(items)):
                    e = store.read([f"{key}#{j}"])[0]
                    if e is None or e.address != f"{key}#{j}":
                        stats["guard"] += 1; return None
                    lats.append(e.latent.to(DEVICE))
                slots, lat = [None] * len(items), torch.stack(lats)
            else:
                slots, lat = [use] + list(ps), None
        true_carry_prev = true_carry
        pred_carry = call_core(m, tok, IDENT, slots, dl, lat)
        stats["calls"] += 1
        if pred_carry is None:
            return None
    stats["calls"] += 1
    return call_core(m, tok, st0, [pred_carry])        # 套回真正的起始狀態


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--Ks", type=int, nargs="+", default=[4, 8, 12])
    ap.add_argument("--confirm", action="store_true",
                    help="confirmatory：只跑 pred_text vs pred_mem_allph，"
                         "新 seed／未觸碰 split，gate 事前鎖死")
    ap.add_argument("--diag", action="store_true",
                    help="加跑 ⑤/⑥ 的 exploratory diagnostic（不改 primary 四組）")
    ap.add_argument("--seed", type=int, default=90909)
    a = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    m, dl, writer, _, _, _ = G3D.load_all(tok, "g3a_writer_6820855741.pth",
                                          "results_g2b.json")
    two = G3B.two_token_keys(tok, [k for k in R.ALL_KEYS if int(k[1:]) < ADDR_DIM])
    _, perms = R.perm_splits()
    G3.WRITE_KEYS = sorted(set(two))
    ev = precompute_events(m, tok, perms)

    print(f"  **全凍結、零訓練**；checkpoint 是**置換**、放在 **value slot**")
    print(f"  （`zdelta` 唯一訓練過的交付角色；不去碰狀態位置）")
    print(f"  每一次 core 呼叫都 **k≤4** —— 單次深度完全沒有被突破\n")

    conds = ("pred_text", "pred_mem_allph") if a.confirm else \
        CONDS + (DIAG if a.diag else ())
    if a.confirm:
        print(f"  **confirmatory**（§4.44）：只跑 `pred_text` vs `pred_mem_allph`，"
              f"新 seed {a.seed}、未觸碰 split")
        print(f"  gate **事前鎖死**：memory 不得低於 text 超過 **5pp**，"
              f"且 **K=8 accuracy ≥ 95%**；K=12 **不另改門檻**\n")
    rng = random.Random(a.seed)
    res = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    calls = defaultdict(lambda: defaultdict(list))
    for K in a.Ks:
        print(f"  ── K={K}   分段 {segments(K)}（每段新做幾步）")
        for _ in range(a.n):
            chain = [list(perms[rng.randrange(len(perms))]) for _ in range(K)]
            st0 = list(IDENT); rng.shuffle(st0)
            gold = st0
            for p in chain:
                gold = compose(gold, p)
            for c in conds:
                st = defaultdict(int)
                if c == "monolithic":
                    got = call_core(m, tok, st0, chain); st["calls"] = 1
                else:
                    got = run_segmented(m, tok, dl, writer, ev, chain, st0, c,
                                        two[0], st)
                r = res[c][K]; r[1] += 1; r[0] += int(got == gold)
                calls[c][K].append(st["calls"])
        print(f"  {'條件':<14s} {'accuracy':>9s} {'core calls':>11s}")
        for c in conds:
            r = res[c][K]; cl = calls[c][K]
            print(f"  {c:<14s} {r[0]/max(r[1],1):8.1%} {sum(cl)/max(len(cl),1):10.1f}")
        print()

    if a.confirm:
        t8 = res["pred_text"][8]; m8 = res["pred_mem_allph"][8]
        acc_t8, acc_m8 = t8[0]/max(t8[1],1), m8[0]/max(m8[1],1)
        g1 = (acc_t8 - acc_m8) <= 0.05
        g2 = acc_m8 >= 0.95
        print(f"  **gate（K=8）**：memory {acc_m8:.1%} vs text {acc_t8:.1%}"
              f"  差 {(acc_t8-acc_m8)*100:+.1f}pp")
        print(f"    ≤5pp {'✅' if g1 else '❌'}   ≥95% {'✅' if g2 else '❌'}"
              f"   →  **{'PASS' if (g1 and g2) else 'FAIL'}**")
        for K in a.Ks:
            if K == 8: continue
            t, mm = res["pred_text"][K], res["pred_mem_allph"][K]
            print(f"    （K={K} 照報，不另設門檻：memory {mm[0]/max(mm[1],1):.1%}"
                  f" vs text {t[0]/max(t[1],1):.1%}）")
        json.dump({"confirm": True, "seed": a.seed, "n": a.n,
                   "cells": {c: {str(K): res[c][K] for K in a.Ks} for c in conds},
                   "gate_k8": {"memory": acc_m8, "text": acc_t8,
                               "verdict": "PASS" if (g1 and g2) else "FAIL"}},
                  open(os.path.join(HERE, "results_g5b_confirm.json"), "w"),
                  indent=2, ensure_ascii=False)
        print(f"  -> results_g5b_confirm.json"); return
    print(f"  判讀（事前鎖死）：")
    print(f"    ①vs③/④  分段計算增益      ②vs③  中間誤差傳播      ③vs④  記憶往返")
    print(f"  ⚠️ 通過只能說「**多次 core 呼叫 + checkpoint 繞過了整體鏈長**」，"
          f"\n     **不能**說單次 executor 變深。monolithic 與 segmented 的**計算量不同**，"
          f"\n     這是**系統能力／compute tradeoff**，不是等算力的模型能力比較。"
          f"\n  ⚠️ 沿用同一 S₅ schema 是合法的 homogeneous closed-type 正控制，"
          f"\n     但結果**可能依賴代數閉包與型別同構**；異質中間狀態另立後續泛化題。")
    allc = conds
    json.dump({"cells": {c: {str(K): res[c][K] for K in a.Ks} for c in allc},
               "calls": {c: {str(K): sum(calls[c][K])/max(len(calls[c][K]),1)
                             for K in a.Ks} for c in allc},
               "n": a.n, "seed": a.seed, "diag": a.diag},
              open(os.path.join(HERE, "results_g5b_diag.json" if a.diag
                                else "results_g5b.json"), "w"), indent=2,
              ensure_ascii=False)
    print(f"  -> results_g5b.json")


if __name__ == "__main__":
    main()
