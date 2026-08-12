"""`B0-N` —— **受控 noisy-description resolver policy**（Codex [159] 回覆鎖定規格）。

**措辭限定**：這是 `noisy symbolic descriptor resolution`，
**不是** semantic／open-set retrieval，PASS 也不得升格。

距離是明確的 Hamming error model，不是相似度：
`d(q,e) = 1[color 不同] + 1[shape 不同]`。
resolver 規則見 `bridge_store.resolve_noisy`，**開跑前鎖死**。

### 三個 stratum（每題逐一 assert 幾何，不靠重抽湊出好看的配置）

| | 幾何條件 | 正確行為 |
|---|---|---|
| `N-U` | target 唯一 `d=1`，次近 `≥2` | 解出唯一 key 並交付 |
| `N-T` | **恰兩個** `d=1` | **hard-abstain** |
| `N-Ø` | 沒有任何 `d<=1` | **hard-abstain** |

### 安全定義（Codex [159]：以「不應交付」定義，不以答案字面鑽漏洞）

`N-T`／`N-Ø` 上**任一** resolver key、**任一** injector call、**任一**模型作答
都算 `unsafe delivery`，**不論答案剛好是 store 裡別條值、target 值或第三值**。
那三種只作診斷欄，不能拿來說「其實沒答錯」。

### PASS

三個 pool `{8,24,36}` 各自三格全過才算通過：
`N-U` exact 300/300 且 0 false-abstain 且與 direct-exact path 逐例相同；
`N-T`／`N-Ø` 各 0/300 unsafe delivery（單側 95% UB 0.99%）。
`N-U` 出現任一次 wrong-existing key → **primary FAIL**。

一次性 run。FAIL 只記「這個固定距離規則不成立」，
**不得**改 distance／margin／比例或 retry 去救。
"""
import json, os, random, sys
import torch
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer
import bridge_latent_schema as S
import bridge_renderer as B
import bridge_store as ST
from bridge_latent_gate import gen
from bridge_g3c_dangling import Injector, cp_upper
from bridge_train_core import DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
POOLS = (8, 24, 36)
STRATA = ("N-U", "N-T", "N-Ø")


def classify(store, qc, qs, attr):
    """回傳 (stratum_or_None, target_name_or_None)。**幾何由 store 現況決定。**"""
    d = sorted(((ST.hamming(qc, qs, nm), nm) for nm in B.NAMES
                if store.contains(nm, attr)))
    if not d or d[0][0] >= 2:
        return "N-Ø", None
    if d[0][0] == 0:
        return None, None                       # 精確命中歸 B0-U，不在本實驗
    ones = [nm for dd, nm in d if dd == 1]
    if len(ones) == 1:
        return "N-U", ones[0]
    if len(ones) == 2:
        return "N-T", None
    return None, None                           # 3 個以上的 d=1 不在鎖定的三格內


def enumerate_queries(store):
    """列舉全部 `8×8×3` 個 query，依 stratum 分組。**窮舉，不抽樣。**"""
    g = {s: [] for s in STRATA}
    for qc in range(ST.N_COLOR):
        for qs in range(ST.N_SHAPE):
            for attr in range(len(B.ATTRS)):
                st, tgt = classify(store, qc, qs, attr)
                if st:
                    g[st].append((qc, qs, attr, tgt))
    return g


@torch.no_grad()
def deliver(m, tok, inj, loops, store, key, dis, tv, dv):
    """解出 key 之後的交付路徑。回傳 (z, delivery tensor, decode)。"""
    z = store.read(*key)
    facts = [(key[0], key[1], tv), (dis[0], dis[1], dv)]
    ep = B.Episode(facts, f"{key[0]} {B.ATTRS[key[1]]} 是 多少", B.val_str(tv), [0], 0)
    mc = inj(torch.stack([z, store.read(*dis)]))
    ids = torch.tensor(tok(tok.bos_token + S.render_latent(ep)[0],
                           add_special_tokens=False).input_ids)
    return z, mc, gen(m, tok, ids, loops, mc)


def main(core="bridge_core_latent.pth", n=300):
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    blob = torch.load(os.path.join(HERE, core), map_location="cpu")
    arch = dict(blob["arch"])
    m = MiniMindForCausalLM(MiniMindConfig(vocab_size=blob["vocab"], **arch)).to(DEVICE).eval()
    m.load_state_dict(blob["model"])
    loops = arch["num_loops"]
    proj = S.CarrierProj(arch["hidden_size"]).to(DEVICE)
    proj.scale.fill_(blob["carrier_scale"])
    inj = Injector(proj)

    print(f"  B0-N  noisy symbolic descriptor resolution   凍結 {core}（**零訓練**）")
    print(f"  prereg: Codex [159]；d(q,e)=1[color≠]+1[shape≠]；規則開跑前鎖死")
    print(f"  pool ∈ {POOLS}，每格 n={n}；active 注入固定 n=2")
    print("  ⚠️ **不是** semantic／open-set retrieval，PASS 亦不得升格\n")

    allp = [(nm, ai) for nm in B.NAMES for ai in range(len(B.ATTRS))]
    res, fails = {}, []
    print(f"  {'pool':>5s} {'stratum':>7s} {'key exact':>11s} {'abstain':>9s} "
          f"{'unsafe交付':>10s} {'逐位元同':>9s} {'診斷 wrong/luck/third':>22s}")
    for pool in POOLS:
        rng = random.Random(60000 + pool)
        desc = rng.sample(allp, pool)
        store = ST.Store()
        vals = {}
        for k in desc:
            vals[k] = rng.randint(B.VMIN, B.VMAX)
            store.commit(k[0], k[1], vals[k])
        assert store.verify() == 0
        groups = enumerate_queries(store)

        for stratum in STRATA:
            q = groups[stratum]
            if not q:
                print(f"  {pool:>5d} {stratum:>7s}   **此 pool 下該 stratum 為空**"
                      f"（結構事實，不掩蓋）")
                res[f"{pool}|{stratum}"] = {"empty": True}
                fails.append(f"pool{pool}/{stratum}:empty")
                continue
            exact = abst = unsafe = same = 0
            diag = {"wrong_existing": 0, "target_by_luck": 0, "third": 0}
            wrong_key = 0
            for i in range(n):
                qc, qs, attr, tgt = q[rng.randrange(len(q))]
                # **逐題 assert 幾何**，不靠重抽湊配置
                st_chk, tgt_chk = classify(store, qc, qs, attr)
                assert st_chk == stratum and tgt_chk == tgt, (st_chk, stratum)

                c0 = inj.calls
                key, status = ST.resolve_noisy(store, qc, qs, attr)

                if stratum == "N-U":
                    if status != "ok":
                        abst += 1                     # N-U 的 abstain = false-abstain
                        continue
                    if key != (tgt, attr):
                        wrong_key += 1                # 任一次即 primary FAIL
                        continue
                    exact += 1
                    dis = [k for k in desc if k != key][
                        rng.randrange(len(desc) - 1)]
                    z, mc, out = deliver(m, tok, inj, loops, store, key, dis,
                                         vals[key], vals[dis])
                    # direct-exact path：用該 entity 真實的 (color, shape) 走 B0-U
                    ec, es = ST.ENT[tgt]
                    k2, s2 = ST.resolve(store, ec, es, attr)
                    z2, mc2, out2 = deliver(m, tok, inj, loops, store, k2, dis,
                                            vals[k2], vals[dis])
                    same += int(s2 == "ok" and k2 == key
                                and torch.equal(z, z2) and torch.equal(mc, mc2)
                                and out == out2)
                else:
                    # N-T／N-Ø：任一 key、任一 injector call、任一作答都算 unsafe
                    if status != "ok":
                        abst += 1
                        assert inj.calls == c0, "abstain 後不得有任何交付"
                        continue
                    unsafe += 1
                    dis = [k for k in desc if k != key][
                        rng.randrange(len(desc) - 1)]
                    _, _, out = deliver(m, tok, inj, loops, store, key, dis,
                                        vals[key], vals[dis])
                    pv = B.val_str(vals[key])
                    if tgt is not None and out == B.val_str(vals[(tgt, attr)]):
                        diag["target_by_luck"] += 1
                    elif out == pv or any(out == B.val_str(v) for v in vals.values()):
                        diag["wrong_existing"] += 1
                    else:
                        diag["third"] += 1

            if stratum == "N-U":
                ok = (exact == n and abst == 0 and same == n and wrong_key == 0)
                cells = f"{exact}/{n}"
                ub = None
            else:
                ok = unsafe == 0
                cells = "—"
                ub = cp_upper(unsafe, n) if unsafe else cp_upper(0, n)
            if not ok:
                fails.append(f"pool{pool}/{stratum}")
            dcol = "{}/{}/{}".format(diag["wrong_existing"],
                                     diag["target_by_luck"], diag["third"])
            scol = f"{same}/{n}" if stratum == "N-U" else "—"
            print(f"  {pool:>5d} {stratum:>7s} {cells:>11s} {f'{abst}/{n}':>9s} "
                  f"{f'{unsafe}/{n}':>10s} {scol:>9s} {dcol:>22s}"
                  + (f"  UB {ub:.2%}" if ub is not None else "")
                  + ("" if ok else "  ✗"))
            res[f"{pool}|{stratum}"] = {"exact": exact, "abstain": abst,
                                        "unsafe": unsafe, "bitwise_same": same,
                                        "wrong_key": wrong_key, "diag": diag,
                                        "n": n, "n_queries": len(q)}

    print()
    if fails:
        print(f"  → **B0-N FAIL**（{', '.join(fails)}）")
        print("    依 prereg：只記「這個固定距離規則不成立」，")
        print("    **不得**改 distance／margin／比例或 retry 去救。")
    else:
        print("  → **B0-N PASS**：受控 noisy-description resolver policy 成立。")
        print("    **不得**升格成 semantic／open-set retrieval；")
        print("    下一個問題是 learned／自然語言 key extraction 的獨立規格。")
    json.dump({"core": core, "n": n, "pools": list(POOLS), "cells": res,
               "fails": fails, "pass": not fails},
              open(os.path.join(HERE, "results_bridge_b0n.json"), "w"),
              indent=2, ensure_ascii=False)
    print("  -> results_bridge_b0n.json")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "bridge_core_latent.pth",
         int(sys.argv[2]) if len(sys.argv) > 2 else 300)
