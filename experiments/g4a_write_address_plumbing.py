"""G4a：**write-address plumbing / positive control** —— 短 gate，不是訓練關。

驗證的管線是：

    literal canonical span → address construction
      → **atomic `(key, address, z)` commit**
      → exact-membership read → delivery → answer

**只證明「可以建立可用的 entry」。**
**不證明** semantic binding、**不證明** content addressing、
**不證明** 未見結構（span 長度）的泛化。

### 職責切法（Codex）—— 這一節的重點其實在這裡

| 元件 | 負責 | 不負責 |
|---|---|---|
| **writer** | 形成內容 `z` | **不輸出 membership bit** |
| **key extractor / address encoder** | 處理 literal key → address | 不碰內容 |
| **store** | **原子綁定** `(key, address, z)` 與 membership | 不做任何猜測 |

⚠️ **不用 MLP 在四個 closed key 上「學 address」再稱之為 formation** ——
   那只是記表。**deterministic / tied mapping 更誠實**，所以這裡的 address
   就是 `address_vector(key)`，**零可學參數**。

### 讓這一關帶一點研究資訊

用**未見但 span 長度固定**的 literal identity（2-token），
並**每個 episode 隨機重配 `key ↔ perm`** ——
測的是 **arbitrary identifier association**：同一個 key 這次配這個置換、
下次配另一個，writer 與 store 不能靠任何「這個 key 通常是什麼」的先驗。
比只用 `f0..f3` 強，但**仍是 exact identity**，且 span 結構固定是為了**隔離 binding**，
**不宣稱** 3-token robustness。
"""
import argparse
import hashlib
import json
import os
import random
import sys

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
from g2c_cal_v2 import cp_upper, wilson
from g3a_train import precompute_events
from model.memory_module import (ADDR_DIM, LATENT_DIM, PERM_N, LatentStore, MemoryEntry,
                                 address_vector)

HERE = os.path.dirname(os.path.abspath(__file__))


def extract_key(tok, view, candidates):
    """**key extractor**：從事件的 literal canonical span 取回 key。

    走 `g1_renderer.locate_key_span` 這個唯一定義來源 —— 前導空白、
    context-dependent 切法全都在那裡處理過（CLAUDE.md 的 gotcha）。
    回傳 `None` 表示抽取失敗，呼叫端必須 fail-closed。
    """
    for k in candidates:
        try:
            R.locate_key_span(tok, view, k)
            return k
        except AssertionError:
            continue
    return None


def address_of(key):
    """**address encoder**：deterministic、**零可學參數**。

    不用 MLP 在少數 closed key 上「學」一個 address —— 那是記表不是 formation。
    """
    return address_vector(key)


@torch.no_grad()
def run(m, tok, dl, writer, ev, s, pool, pool_perm, omitted, keys, stats):
    store = LatentStore()
    for x in pool:
        view = R.render_define_view(x, pool_perm[x])
        key = extract_key(tok, view, keys)               # ← 從事件抽 key
        stats["extract_n"] += 1
        stats["extract_ok"] += int(key == x)
        if key is None:
            stats["extract_fail"] += 1
            continue
        z = writer(ev[(x, tuple(pool_perm[x]))].unsqueeze(0))[0]   # ← writer 只做內容
        # **原子綁定**：key、address、內容一起進 store，缺一不可
        store.commit([MemoryEntry(key, z, {"key": key,
                                           "addr": address_of(key).tolist()})])

    # address↔content binding：每條 entry 的 address 必須由它自己的 key 決定
    for x, e in store._entries.items():
        stats["bind_n"] += 1
        stats["bind_ok"] += int(e.address == x
                                and torch.allclose(torch.tensor(e.metadata["addr"]),
                                                   address_of(x)))

    lat = []
    for key in s.chain:
        if key not in store._entries:                    # membership 由 store 回答
            return dict(abstained=True, e2e=None)
        e = store.read([key])[0]
        if e is None or e.address != key or e.latent.shape != (LATENT_DIM,):
            return dict(abstained=True, e2e=None)
        lat.append(e.latent.to(DEVICE))
    _, b_ids, pos = value_positions(tok, s)
    got = greedy_override(m, tok, b_ids,
                          G3._mk_fn(dl, torch.stack(lat).unsqueeze(0), pos.to(DEVICE)),
                          ARCH["num_loops"])
    return dict(abstained=False, e2e=(got == R.render_L0(s)[1]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-missing", type=int, default=200)
    ap.add_argument("--n-answerable", type=int, default=200)
    ap.add_argument("--k", type=int, default=1)
    ap.add_argument("--pool", type=int, default=8)
    ap.add_argument("--seed", type=int, default=51515)
    ap.add_argument("--writer-ckpt", default="g3a_writer_6820855741.pth")
    ap.add_argument("--g2b-results", default="results_g2b.json")
    a = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    m, dl, writer, _, _, _ = G3D.load_all(tok, a.writer_ckpt, a.g2b_results)
    addressable = [k for k in R.ALL_KEYS if int(k[1:]) < ADDR_DIM]
    two = G3B.two_token_keys(tok, addressable)
    # **未見的 query identity**（排除 G1/G2/G3 一路用的 f0..f3），span 長度固定為 2
    unseen = [k for k in two if k not in R.KEYS]
    assert len(unseen) > a.pool
    _, perms = R.perm_splits()
    G3.WRITE_KEYS = sorted(set(two))
    ev = precompute_events(m, tok, perms)

    print(f"  **全凍結、零訓練**；address = `address_vector(key)`，**零可學參數**")
    print(f"  職責切法：writer 只做內容 `z`；key extractor 處理 literal key；"
          f"store 負責原子綁定與 membership")
    print(f"  query identity = **未見的 {len(unseen)} 個 2-token key**"
          f"（排除一路用到的 {R.KEYS}）")
    print(f"  每個 episode **隨機重配 key↔perm** —— 測 arbitrary identifier association\n")

    rng = random.Random(a.seed)
    miss = [G3B.make_episode(a.k, rng, unseen, two, perms, a.pool, 1.0)
            for _ in range(a.n_missing)]
    ans = [G3B.make_episode(a.k, rng, unseen, two, perms, a.pool, 0.0)
           for _ in range(a.n_answerable)]
    chk = hashlib.sha256("".join(e[0].delivery_id for e in miss + ans).encode()
                         ).hexdigest()[:16]
    print(f"  {len(miss)} missing + {len(ans)} answerable   checksum {chk}\n")

    st = {"extract_n": 0, "extract_ok": 0, "extract_fail": 0, "bind_n": 0, "bind_ok": 0}
    R_ab = hal = 0
    for s, pool, pp, om in miss:
        r = run(m, tok, dl, writer, ev, s, pool, pp, om, two, st)
        R_ab += int(r["abstained"]); hal += int(not r["abstained"])
    fa = ok = 0
    for s, pool, pp, om in ans:
        r = run(m, tok, dl, writer, ev, s, pool, pp, om, two, st)
        fa += int(r["abstained"]); ok += int(bool(r["e2e"]))

    nm, na = len(miss), len(ans)
    print(f"  **管線各環**")
    print(f"    key extraction exact     {st['extract_ok']/max(st['extract_n'],1):6.1%} "
          f"({st['extract_ok']}/{st['extract_n']})   抽取失敗 {st['extract_fail']}")
    print(f"    address↔content binding  {st['bind_ok']/max(st['bind_n'],1):6.1%} "
          f"({st['bind_ok']}/{st['bind_n']})")
    print(f"\n  **missing（n={nm}）**   R_abstain {R_ab/nm:6.1%}   "
          f"halluc {hal/nm:6.1%} ({hal}/{nm})  UB {cp_upper(hal, nm):.2%}")
    print(f"  **answerable（n={na}）** A_ans     {ok/na:6.1%} "
          f"[{wilson(ok, na)[0]:.1%}, {wilson(ok, na)[1]:.1%}]   "
          f"false_abstain {fa/na:6.1%}")
    passed = (st["extract_ok"] == st["extract_n"] and st["bind_ok"] == st["bind_n"]
              and hal == 0 and fa == 0)
    print(f"\n  裁決：**{'PASS —— write-address plumbing' if passed else 'FAIL'}**")
    print(f"  ⚠️ 只證明**可以建立可用的 entry**。**不證明** semantic binding、"
          f"\n     **不證明** content addressing、**不證明**未見 span 結構的泛化。"
          f"\n     address 是 deterministic 的（`address_vector`），**沒有任何東西被學到**——"
          f"\n     用 MLP 在少數 closed key 上學 address 只是記表，那不叫 formation。")
    json.dump({"k": a.k, "pool": a.pool, "seed": a.seed, "checksum": chk,
               "unseen_identities": unseen, "pipeline": st,
               "R_abstain": R_ab, "halluc": hal, "A_ans": ok, "false_abstain": fa,
               "verdict": "PASS" if passed else "FAIL",
               "milestone": "write-address plumbing / positive control"},
              open(os.path.join(HERE, "results_g4a.json"), "w"), indent=2,
              ensure_ascii=False)
    print(f"  -> results_g4a.json")


if __name__ == "__main__":
    main()
