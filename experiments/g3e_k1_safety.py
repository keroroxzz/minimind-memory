"""G3e：k=1 的安全數字複驗 —— **只跑這一次**，全凍結、不重校、不選 seed。

理由：`k=1` 的 `halluc` **2/32**（§4.36）與 G3b 的 **3/59**（§4.34）
已經是**兩次小樣本非零**，不宜直接當噪聲（Codex）。

**分層固定**，不靠 15% 隨機去湊樣本：

    **300 個 missing + 300 個 answerable**，全新 episodes，
    事前存 IDs 與 checksum，直接收斂 conditional 的風險與效用。

預鎖的報告內容（跑之前定，跑之後不改）：

  missing 條件     `R_abstain`、`halluc` —— **`halluc` 給單側 95% Clopper–Pearson 上界**
  answerable 條件  `false_abstain`、`A_ans` —— 雙側 CI

⚠️ **不設新 threshold、不選 seed、不重跑。**
⚠️ 若 `halluc` 仍非零 → 記為「closed-world support 有**稀有失敗**」。
   若 0/300 → 只能說**與 ≤ 約 1% 的上界相容**，**不抹掉**舊的 2/32 與 3/59。
⚠️ **只跑 k=1。** 不得宣稱「k=1 因 context 短而特別差」——
   那是跨 k 的機制比較，需要 matched 大 n 的 k=2 對照。
"""
import argparse
import hashlib
import json
import math
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
from g2c_cal_v2 import cp_upper, wilson
from g3a_train import precompute_events
from model.memory_module import ADDR_DIM

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-missing", type=int, default=300)
    ap.add_argument("--n-answerable", type=int, default=300)
    ap.add_argument("--k", type=int, default=1)
    ap.add_argument("--pool", type=int, default=8)
    ap.add_argument("--seed", type=int, default=20260804)
    ap.add_argument("--writer-ckpt", default="g3a_writer_6820855741.pth")
    ap.add_argument("--g2b-results", default="results_g2b.json")
    a = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    m, dl, writer, ret, thr, ck = G3D.load_all(tok, a.writer_ckpt, a.g2b_results)
    addressable = [k for k in R.ALL_KEYS if int(k[1:]) < ADDR_DIM]
    two = G3B.two_token_keys(tok, addressable)
    query_keys = [k for k in R.KEYS if k in two]
    _, perms = R.perm_splits()
    G3.WRITE_KEYS = sorted(set(query_keys) | set(two))
    ev = precompute_events(m, tok, perms)

    # ---- 分層固定：不靠 p_omit 隨機 ----
    rng = random.Random(a.seed)
    miss = [G3B.make_episode(a.k, rng, query_keys, two, perms, a.pool, 1.0)
            for _ in range(a.n_missing)]
    ans = [G3B.make_episode(a.k, rng, query_keys, two, perms, a.pool, 0.0)
           for _ in range(a.n_answerable)]
    assert all(e[3] is not None for e in miss) and all(e[3] is None for e in ans)

    ids = [e[0].delivery_id for e in miss + ans]
    pre = {"k": a.k, "pool": a.pool, "seed": a.seed, "threshold": thr,
           "n_missing": len(miss), "n_answerable": len(ans),
           "writer": a.writer_ckpt, "retriever": ck,
           "episode_checksum": hashlib.sha256("".join(ids).encode()).hexdigest()[:16],
           "prelocked": "只報 R_abstain/halluc（halluc 給單側 95% CP 上界）與 "
                        "false_abstain/A_ans（雙側 CI）；不設新 threshold、不選 seed、不重跑"}
    json.dump(pre, open(os.path.join(HERE, "g3e_prereg.json"), "w"),
              indent=2, ensure_ascii=False)
    print(f"  **全凍結、不重校、不選 seed、只跑一次**（threshold {thr:+.3f} 沿 G2b）")
    print(f"  k={a.k}, pool={a.pool}；**分層固定** {len(miss)} missing + {len(ans)} answerable")
    print(f"  episodes checksum {pre['episode_checksum']}（**在評測之前**已落 g3e_prereg.json）\n")

    R_ab = hal = 0
    with torch.no_grad():
        for s, pool, pp, om in miss:
            r = G3B.run_episode(m, tok, dl, writer, ret, ev, s, pool, pp, om, thr,
                                True, True, True, None)
            R_ab += int(r["abstained"]); hal += int(not r["abstained"])
        fa = ok = 0
        for s, pool, pp, om in ans:
            r = G3B.run_episode(m, tok, dl, writer, ret, ev, s, pool, pp, om, thr,
                                True, True, True, None)
            fa += int(r["abstained"]); ok += int(bool(r["e2e"]))

    nm, na = len(miss), len(ans)
    print(f"  **missing 條件**（n={nm}）")
    print(f"    R_abstain      {R_ab/nm:6.1%}  [{wilson(R_ab, nm)[0]:.1%}, "
          f"{wilson(R_ab, nm)[1]:.1%}]")
    print(f"    halluc         {hal/nm:6.1%}  ({hal}/{nm})   "
          f"**單側 95% CP 上界 {cp_upper(hal, nm):.2%}**")
    print(f"\n  **answerable 條件**（n={na}）")
    print(f"    A_ans          {ok/na:6.1%}  [{wilson(ok, na)[0]:.1%}, {wilson(ok, na)[1]:.1%}]")
    print(f"    false_abstain  {fa/na:6.1%}  [{wilson(fa, na)[0]:.1%}, {wilson(fa, na)[1]:.1%}]")
    print(f"\n  對照（小樣本，**不被本次抹掉**）：§4.36 k=1 halluc 2/32；§4.34 G3b 3/59")
    if hal:
        print(f"  → **closed-world support 有稀有失敗**（{hal}/{nm}）。")
    else:
        print(f"  → 0/{nm}，只能說**與 ≤ {cp_upper(0, nm):.2%} 的上界相容**，"
              f"不等於「不會發生」。")
    print(f"  ⚠️ 只跑 k=1。**不得**宣稱「k=1 因 context 短而特別差」——"
          f"那需要 matched 大 n 的 k=2 對照。")
    json.dump({**pre, "R_abstain": R_ab, "halluc": hal,
               "halluc_ub95": cp_upper(hal, nm), "A_ans": ok, "false_abstain": fa},
              open(os.path.join(HERE, "results_g3e_k1.json"), "w"), indent=2,
              ensure_ascii=False)
    print(f"  -> results_g3e_k1.json")


if __name__ == "__main__":
    main()
