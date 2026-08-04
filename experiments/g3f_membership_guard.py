"""G3f：**exact-membership guarded closure** —— 把 membership 判定搬回 store 契約。

§4.37 量到 learned support 有約 **2.7%** 的稀有 false-accept。但在本任務裡
**query 攜帶 canonical exact key、store 也以同一個 key commit** ——
`contains(key)` 是**可判定的資料結構事實**，用學到的相似度去猜它是**職責錯置**（Codex）。

**權威路徑**（learned support **不得覆蓋**它）：

    canonicalize(query key)
      → store.contains(key)
          absent  → **硬 abstain**（連 retrieve 都不允許）
          present → 允許 retrieve
      → read 後 assert「取回的 entry key 一致 且 內容確實 committed」
          不一致 → **fail-closed**

learned support 降為 **shadow metric**：照算、照報，但**不參與決策**。
它日後只在 semantic / fuzzy query 才有位置。

⚠️ **這不解決 open-set 的語意問題。** 若 query 沒有可靠的 canonical key、
   key extraction 出錯、或要找的是「語意相關」而非同一個 ID，store 無法直接回答。
   **§4.30 的 G2c calibration FAIL 仍是完整系統的 blocker**，
   不因這裡結構性零 halluc 而回填。
⚠️ 本結果另立里程碑 **`exact-membership guarded closure`**，
   §4.37 的 8/300 **原樣保留**。

近乎 exhaustive 的契約測試，三項全過才封板：
  A. **present 不誤擋** —— 有寫進去的 key 不得被 guard 擋下
  B. **absent 零交付** —— 沒寫進去的 key 不得產生任何交付
  C. **wrong-key read fail-closed** —— 取回的 entry 與請求不符時必須中止
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
from g2_train import core_hidden_cue
from g2c_cal_v2 import cp_upper, wilson
from g3a_train import precompute_events
from model.memory_module import (ADDR_DIM, LATENT_DIM, PERM_N, LatentStore, MemoryEntry,
                                 address_vector)

HERE = os.path.dirname(os.path.abspath(__file__))


class GuardAbort(Exception):
    """契約不成立。**唯一可接受的動作是 abstain。**"""


@torch.no_grad()
def run_guarded(m, tok, dl, writer, ret, ev, s, pool, pool_perm, omitted, thr, stats,
                corrupt_key=False):
    store = LatentStore()
    for x in pool:
        z = writer(ev[(x, tuple(pool_perm[x]))].unsqueeze(0))[0]
        store.commit([MemoryEntry(x, z, {"key": x})])
    if corrupt_key:
        # ⚠️ 契約 C 的**刻意注入**：自然評測裡 `wrongkey` 從未觸發（retriever 沒選錯），
        #    那只證明「guard 沒掩蓋檢索錯誤」，**不證明 fail-closed 分支有效**（Codex）。
        #    這裡把某個 entry 的 address 欄改成別的 key，讓 read 回來的東西
        #    與請求不符 —— 必須被擋下、零交付。
        victim = s.chain[0]
        other = next(x for x in pool if x != victim)
        e = store._entries[victim]
        store._entries[victim] = MemoryEntry(other, e.latent.clone(), e.metadata, e.version)

    addrs = torch.stack([address_vector(x) for x in pool]).to(DEVICE)
    cues = core_hidden_cue(m, tok, s)
    logits, sup = ret(cues.unsqueeze(0), addrs.unsqueeze(0))
    pred = logits[0].argmax(-1).tolist()
    shadow_hit = (sup[0] > thr).tolist()          # **shadow：照算，不參與決策**

    lat = []
    try:
        for j, key in enumerate(s.chain):
            # ---- 權威：membership 由 store 回答，不由學到的分數猜 ----
            if key not in store._entries:
                stats["guard_absent"] += 1
                raise GuardAbort(f"{key} 不在 store 裡")
            e = store.read([key])[0]
            if e is None or e.address != key or e.latent.shape != (LATENT_DIM,):
                # `e.address != key` 就是契約 C：取回的 entry 與請求不符
                stats["guard_badread"] += 1
                raise GuardAbort(f"{key} 的讀取違反契約")
            # ---- learned retriever 仍負責選址，但要被 guard 覆核 ----
            if pool[pred[j]] != key:
                stats["guard_wrongkey"] += 1      # C：取回的 entry 與請求不符
                raise GuardAbort(f"retriever 選了 {pool[pred[j]]}，請求的是 {key}")
            lat.append(e.latent.to(DEVICE))
    except GuardAbort:
        return dict(abstained=True, e2e=None, shadow_hit=shadow_hit)

    _, b_ids, pos = value_positions(tok, s)
    got = greedy_override(m, tok, b_ids,
                          G3._mk_fn(dl, torch.stack(lat).unsqueeze(0), pos.to(DEVICE)),
                          ARCH["num_loops"])
    return dict(abstained=False, e2e=(got == R.render_L0(s)[1]), shadow_hit=shadow_hit)


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

    # 與 §4.37 **完全同一批 episodes**（同 seed、同分層），才比得出 guard 的效果
    rng = random.Random(a.seed)
    miss = [G3B.make_episode(a.k, rng, query_keys, two, perms, a.pool, 1.0)
            for _ in range(a.n_missing)]
    ans = [G3B.make_episode(a.k, rng, query_keys, two, perms, a.pool, 0.0)
           for _ in range(a.n_answerable)]
    chk = hashlib.sha256("".join(e[0].delivery_id for e in miss + ans).encode()
                         ).hexdigest()[:16]
    print(f"  **全凍結、零訓練**；threshold {thr:+.3f} 只作 **shadow**，不參與決策")
    print(f"  k={a.k}, pool={a.pool}；{len(miss)} missing + {len(ans)} answerable"
          f"   checksum {chk}")
    print(f"  （與 §4.37 同一批 episodes —— 那次 checksum bda3b054da96ab46）\n")

    st = {"guard_absent": 0, "guard_badread": 0, "guard_wrongkey": 0}
    R_ab = hal = shadow_fa = 0
    for s, pool, pp, om in miss:
        r = run_guarded(m, tok, dl, writer, ret, ev, s, pool, pp, om, thr, st)
        R_ab += int(r["abstained"]); hal += int(not r["abstained"])
        shadow_fa += int(all(r["shadow_hit"]))       # shadow 會不會誤放行
    fa = ok = shadow_wrong = 0
    for s, pool, pp, om in ans:
        r = run_guarded(m, tok, dl, writer, ret, ev, s, pool, pp, om, thr, st)
        fa += int(r["abstained"]); ok += int(bool(r["e2e"]))
        shadow_wrong += int(not all(r["shadow_hit"]))

    nm, na = len(miss), len(ans)
    print(f"  **missing（n={nm}）**")
    print(f"    R_abstain      {R_ab/nm:6.1%}  [{wilson(R_ab, nm)[0]:.1%}, "
          f"{wilson(R_ab, nm)[1]:.1%}]")
    print(f"    halluc         {hal/nm:6.1%}  ({hal}/{nm})   "
          f"單側 95% CP 上界 {cp_upper(hal, nm):.2%}")
    print(f"    ↳ shadow（learned support 若當家）誤放行 {shadow_fa}/{nm} = "
          f"{shadow_fa/nm:.1%}   ← §4.37 量到的就是這個")
    print(f"\n  **answerable（n={na}）**")
    print(f"    A_ans          {ok/na:6.1%}  [{wilson(ok, na)[0]:.1%}, {wilson(ok, na)[1]:.1%}]")
    print(f"    false_abstain  {fa/na:6.1%}  [{wilson(fa, na)[0]:.1%}, {wilson(fa, na)[1]:.1%}]")
    print(f"    ↳ shadow 誤擋 {shadow_wrong}/{na}")
    print(f"\n  **契約測試**")
    print(f"    A. present 不誤擋      false_abstain {fa}/{na}"
          f"{'  ✅' if fa == 0 else '  ❌'}")
    print(f"    B. absent 零交付        halluc {hal}/{nm}{'  ✅' if hal == 0 else '  ❌'}")
    print(f"    C. wrong-key fail-closed  自然評測觸發 {st['guard_wrongkey']} 次"
          f"\n       ⚠️ 這**只**表示 retriever 沒選錯 → guard 沒掩蓋檢索錯誤；"
          f"\n          **不證明 fail-closed 分支有效**，那要靠下面的刻意注入")
    print(f"       guard 明細：absent {st['guard_absent']} / badread "
          f"{st['guard_badread']} / wrongkey {st['guard_wrongkey']}")
    # ---- 契約 C 的刻意注入測試（不需重跑 300 題）----
    cst = {"guard_absent": 0, "guard_badread": 0, "guard_wrongkey": 0}
    c_delivered = 0
    n_c = min(60, len(ans))
    for s_, pool, pp, om in ans[:n_c]:
        r = run_guarded(m, tok, dl, writer, ret, ev, s_, pool, pp, om, thr, cst,
                        corrupt_key=True)
        c_delivered += int(not r["abstained"])
    print(f"\n  **契約 C 的刻意注入**（把 read 回來的 entry key 換錯，n={n_c}）")
    print(f"    交付次數 {c_delivered}/{n_c}{'  ✅ 零交付' if c_delivered == 0 else '  ❌'}"
          f"   guard 觸發：badread {cst['guard_badread']}")
    sealed = (fa == 0 and hal == 0 and c_delivered == 0)
    print(f"\n  裁決：**{'SEALED —— exact-membership guarded closure' if sealed else 'FAIL'}**")
    print(f"  ⚠️ 這**不解決 open-set 的語意問題**：query 沒有可靠 canonical key、"
          f"\n     key extraction 出錯、或要找語意相關而非同 ID 時，store 無法直接回答。"
          f"\n     **§4.30 的 G2c calibration FAIL 仍是完整系統的 blocker**，不因此回填。"
          f"\n     §4.37 的 8/300 原樣保留。")
    json.dump({"k": a.k, "pool": a.pool, "seed": a.seed, "checksum": chk,
               "threshold_shadow_only": thr, "R_abstain": R_ab, "halluc": hal,
               "halluc_ub95": cp_upper(hal, nm), "A_ans": ok, "false_abstain": fa,
               "shadow_false_accept": shadow_fa, "shadow_false_block": shadow_wrong,
               "guard": st, "contract_c_injected_n": n_c,
               "contract_c_delivered": c_delivered,
               "verdict": "SEALED" if sealed else "FAIL",
               "milestone": "exact-membership guarded closure"},
              open(os.path.join(HERE, "results_g3f.json"), "w"), indent=2,
              ensure_ascii=False)
    print(f"  -> results_g3f.json")


if __name__ == "__main__":
    main()
