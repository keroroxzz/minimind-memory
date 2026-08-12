"""`B0-N` **exhaustive coverage audit** —— 非 gate、非 retry（Codex [160] 回覆追加）。

`bridge_b0n_eval.py` 已經窮舉分類過全部 `8×8×3 = 192` 個 query，
但主 gate 是**有放回抽樣 300 次**，所以「每一個相異 query 都被實際 exercise 過」
從來沒有被證明。本檔補上這個 coverage 證明，**只此而已**：

- **不改**模型、seed、distance、margin、比例、門檻。
- **不重跑** 300 episode，也**不**產生新的 PASS／FAIL 判定。
- 固定沿用 `bridge_b0n_eval.py` 的三個 store artifact（同一 seed 重建）。

對每個 `pool × stratum` 的**每一個**相異 `(qc, qs, attr)`：

- assert resolver 回到該 stratum
- `N-U`：assert key 正確
- `N-T`／`N-Ø`：assert 無 key，且 **injector 呼叫增量為 0**

`pool=36 / N-Ø` 因此必須明列為 **2/2 distinct queries covered**。
"""
import json, os, random, sys
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
import bridge_renderer as B
import bridge_store as ST
from bridge_b0n_eval import POOLS, STRATA, classify, enumerate_queries

HERE = os.path.dirname(os.path.abspath(__file__))


class CountingResolver:
    """代理 injector：這個 audit 不做交付，只證明 abstain 路徑不會呼叫它。"""

    def __init__(self):
        self.calls = 0

    def __call__(self, *a, **k):                     # pragma: no cover
        self.calls += 1
        raise AssertionError("coverage audit 不應該有任何交付")


def rebuild(pool):
    """逐字重建 `bridge_b0n_eval.main` 的 store artifact（同一 seed、同一順序）。"""
    allp = [(nm, ai) for nm in B.NAMES for ai in range(len(B.ATTRS))]
    rng = random.Random(60000 + pool)
    desc = rng.sample(allp, pool)
    store, vals = ST.Store(), {}
    for k in desc:
        vals[k] = rng.randint(B.VMIN, B.VMAX)
        store.commit(k[0], k[1], vals[k])
    assert store.verify() == 0
    return store, desc, vals


def main():
    print("  B0-N exhaustive coverage audit（**非 gate、非 retry**，Codex [160]）")
    print("  對每個 pool×stratum 的**每一個**相異 query 實際呼叫 resolver 並 assert")
    print("  不改模型／seed／distance／margin／比例／門檻，不重跑 300 episode\n")
    print(f"  {'pool':>5s} {'stratum':>7s} {'covered':>12s} {'交付次數':>8s}")

    inj = CountingResolver()
    out, bad = {}, []
    for pool in POOLS:
        store, desc, vals = rebuild(pool)
        groups = enumerate_queries(store)
        for stratum in STRATA:
            qs = groups[stratum]
            done = 0
            for qc, qsh, attr, tgt in qs:
                st_chk, tgt_chk = classify(store, qc, qsh, attr)
                assert st_chk == stratum and tgt_chk == tgt
                key, status = ST.resolve_noisy(store, qc, qsh, attr)
                if stratum == "N-U":
                    if not (status == "ok" and key == (tgt, attr)):
                        bad.append((pool, stratum, qc, qsh, attr, status, key))
                        continue
                else:
                    if not (status != "ok" and key is None):
                        bad.append((pool, stratum, qc, qsh, attr, status, key))
                        continue
                    assert inj.calls == 0, "abstain 路徑不得有任何交付"
                done += 1
            print(f"  {pool:>5d} {stratum:>7s} {f'{done}/{len(qs)}':>12s} "
                  f"{inj.calls:>8d}"
                  + ("" if done == len(qs) else "  ✗"))
            out[f"{pool}|{stratum}"] = {"covered": done, "distinct": len(qs)}

    print(f"\n  交付總次數 {inj.calls}（必須為 0）")
    print("  ⚠️ 這是 coverage 證明，**不產生新的 PASS／FAIL**；")
    print("     主 gate 仍是 `bridge_b0n_eval.py` 的三 pool × 三格 0/300。")
    thin = {k: v for k, v in out.items() if v["distinct"] <= 11}
    print(f"\n  **有限 support 的格子**（相異 query ≤ 11，引用時必須帶此限制）：")
    for k, v in sorted(thin.items(), key=lambda x: x[1]["distinct"]):
        print(f"    {k:>12s}  {v['covered']}/{v['distinct']} distinct queries covered")
    if bad:
        print(f"\n  ✗ 不符 {len(bad)} 筆：{bad[:5]}")
    json.dump({"coverage": out, "injector_calls": inj.calls,
               "mismatches": bad, "finite_support": thin},
              open(os.path.join(HERE, "results_bridge_b0n_coverage.json"), "w"),
              indent=2, ensure_ascii=False)
    print("  -> results_bridge_b0n_coverage.json")


if __name__ == "__main__":
    main()
