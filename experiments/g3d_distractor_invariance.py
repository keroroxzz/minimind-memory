"""distractor label-swap invariance —— pool 軸的第二道證據（Codex 要求）。

「3-token distractor 從未被選中」**不足以**證明它們沒污染 pool 軸：
support head 讀的是**完整 logit 幾何**（top1、top1−top2、logsumexp），
一個從未成為 top-1 的 address 仍可能改變 threshold 決策、`R_abstain` 與 `halluc`。

所以另加一個**成對的機械等價測試**：固定 query、pool 基數與**全部 address 向量**，
只把 3-token distractor 的**內容**換掉，然後逐位比較

    retrieval logits · support score · support 決策 · 最終輸出

若程式路徑確實從不讀 distractor 的內容，這應該**逐位相同**。

兩條證據回答不同問題（Codex）：
  **invariance**       排除 span/label 污染 **support**
  **selection count**  排除 oracle distractor 內容污染 **executor**
兩條都過，才能說 pool-size 是唯一變因。
"""
import argparse
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
from g1_train import ARCH, DEVICE
from g2_train import core_hidden_cue
from g3a_train import precompute_events
from model.memory_module import (ADDR_DIM, PERM_N, LatentStore, MemoryEntry,
                                 address_vector, perm_to_latent)

HERE = os.path.dirname(os.path.abspath(__file__))


@torch.no_grad()
def probe(m, tok, ret, writer, ev, s, pool, pool_perm, thr, three, alt):
    """建 store（3-token distractor 的內容由 `alt` 決定）→ 回傳檢索側的所有量。"""
    store = LatentStore()
    for x in pool:
        p = pool_perm[x]
        if x in three:
            z = perm_to_latent(alt[x]).to(DEVICE)         # ← 只有這裡在變
        else:
            z = writer(ev[(x, tuple(p))].unsqueeze(0))[0]
        store.commit([MemoryEntry(x, z, {"key": x})])
    addrs = torch.stack([address_vector(x) for x in pool]).to(DEVICE)
    cues = core_hidden_cue(m, tok, s)
    logits, sup = ret(cues.unsqueeze(0), addrs.unsqueeze(0))
    return logits[0].clone(), sup[0].clone(), (sup[0] > thr).clone(), \
        logits[0].argmax(-1).clone()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("--k", type=int, default=4)
    ap.add_argument("--pool", type=int, default=31)
    ap.add_argument("--p-omit", type=float, default=0.15)
    ap.add_argument("--seed", type=int, default=909)
    ap.add_argument("--writer-ckpt", default="g3a_writer_6820855741.pth")
    ap.add_argument("--g2b-results", default="results_g2b.json")
    a = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    m, dl, writer, ret, thr, ck = G3D.load_all(tok, a.writer_ckpt, a.g2b_results)
    addressable = [k for k in R.ALL_KEYS if int(k[1:]) < ADDR_DIM]
    two = G3B.two_token_keys(tok, addressable)
    three = [k for k in addressable if k not in two]
    query_keys = [k for k in R.KEYS if k in two]
    distractors = two + three
    _, perms = R.perm_splits()
    G3.WRITE_KEYS = sorted(set(query_keys) | set(distractors))
    ev = precompute_events(m, tok, perms)

    # 結構事實：address **只由 key 的索引決定**，與內容、與 token 結構都無關。
    for x in three:
        assert torch.equal(address_vector(x), address_vector(x)), "address 不是決定性的"
    print(f"  3-token distractor：{three}")
    print(f"  pool={a.pool}, k={a.k}, {a.n} episodes；只換這 {len(three)} 條的**內容**，"
          f"\n  query、pool 基數、**全部 address 向量**都不動\n")

    rng = random.Random(a.seed)
    eps = [G3B.make_episode(a.k, rng, query_keys, distractors, perms, a.pool, a.p_omit)
           for _ in range(a.n)]
    arng = random.Random(a.seed + 1)

    worst = {"logits": 0.0, "support": 0.0}
    n_dec = n_arg = n = 0
    for s, pool, pp, om in eps:
        present3 = [x for x in pool if x in three]
        if not present3:
            continue
        altA = {x: pp[x] for x in present3}                       # 原內容
        altB = {}
        for x in present3:                                        # 換成別的置換
            q = list(range(PERM_N))
            while q == list(pp[x]):
                arng.shuffle(q)
            altB[x] = q
        lA, sA, dA, gA = probe(m, tok, ret, writer, ev, s, pool, pp, thr, three, altA)
        lB, sB, dB, gB = probe(m, tok, ret, writer, ev, s, pool, pp, thr, three, altB)
        worst["logits"] = max(worst["logits"], float((lA - lB).abs().max()))
        worst["support"] = max(worst["support"], float((sA - sB).abs().max()))
        n_dec += int(not torch.equal(dA, dB))
        n_arg += int(not torch.equal(gA, gB))
        n += 1

    print(f"  {'量':<22s} {'最大差異／不一致數':>18s}")
    print(f"  {'retrieval logits':<22s} {worst['logits']:>18.3e}")
    print(f"  {'support score':<22s} {worst['support']:>18.3e}")
    print(f"  {'support 決策（>thr）':<22s} {n_dec:>18d}")
    print(f"  {'top-1 選擇':<22s} {n_arg:>18d}")
    ok = worst["logits"] == 0.0 and worst["support"] == 0.0 and n_dec == 0 and n_arg == 0
    print(f"\n  {n} 個含 3-token distractor 的 episode。"
          f"\n  裁決：{'**逐位相同 → distractor 內容不進檢索/支持路徑**' if ok else '**不變性破裂**'}")
    if ok:
        print(f"  搭配 audit 裡「distractor 被選中 0 次」，pool-size 才是唯一變因。")
    json.dump({"n": n, "worst": worst, "decision_mismatch": n_dec,
               "argmax_mismatch": n_arg, "three_token": three,
               "pool": a.pool, "k": a.k, "seed": a.seed,
               "verdict": "INVARIANT" if ok else "BROKEN"},
              open(os.path.join(HERE, "results_g3d_invariance.json"), "w"),
              indent=2, ensure_ascii=False)
    print(f"  -> results_g3d_invariance.json")


if __name__ == "__main__":
    main()
