"""A 路 —— exact-key retrieval ＋ typed membership guard。`BRIDGE_PREREG_retrieval.md`。

**用 §4.55 的凍結 core，不重訓。** retriever 在 core 之外，
所以「別人塞給它的記憶集合」換成「retriever 選出來的集合」對 core 是同一件事。

權威路徑（**不是**學出來的相似度）：

    canonicalize(name, attr) → store.contains(key)
      absent  → **注入之前**硬 abstain（fail closed）→ halluc 結構性為 0
      present → read → 斷言 addr 相符 → 注入 n=2 → core 作答

事前預測：**exact `contains` 的正確率應隨 pool 平坦**；
不平坦就是 store／index 實作問題，**不是容量曲線**（Codex [144]）。
"""
import json
import os
import random
import sys
import time

import torch

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import bridge_latent_schema as S
import bridge_renderer as B
import bridge_store as ST
from bridge_latent_gate import gen, wilson
from bridge_train_core import DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
# ⚠️ **規格修正（smoke 發現，正式結果之前）**：Codex [144] 給的 pool ∈ {8,24,48}
#    在 `pool=48` 時 `absent` 集合是空的 —— 整個世界只有 48 個 descriptor，
#    全部寫入後**一次 miss 都測不到**，而 missing 安全性正是 A 路的重點。
#    修法：保留 `HOLDOUT` 個 descriptor **永不寫入任何 store**，專供 miss 查詢；
#    pool 因此最大為 48 − HOLDOUT。這是**結構限制**，不是挑好看的設定。
HOLDOUT = 12
POOLS = (8, 24, 36)     # 36 = 48 − HOLDOUT
N_ACTIVE = 2            # primary，固定；不得在掃 pool 時一起改
ALL_DESC = [(nm, ai) for nm in B.NAMES for ai in range(len(B.ATTRS))]


def make_ep(target, distractor, tv, dv):
    """由 store 內容組出 episode（j=0）。**答案來自 store，不是重新亂數。**"""
    facts = [(target[0], target[1], tv), (distractor[0], distractor[1], dv)]
    q = f"{target[0]} {B.ATTRS[target[1]]} 是 多少"
    return B.Episode(facts, q, B.val_str(tv), [0], 0)


@torch.no_grad()
def main(n=250, core="bridge_core_latent.pth"):
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    blob = torch.load(os.path.join(HERE, core), map_location="cpu")
    arch = dict(blob["arch"])
    cfg = MiniMindConfig(vocab_size=blob["vocab"], **arch)
    m = MiniMindForCausalLM(cfg).to(DEVICE).eval()
    m.load_state_dict(blob["model"])
    loops = arch["num_loops"]
    proj = S.CarrierProj(arch["hidden_size"]).to(DEVICE)
    proj.scale.fill_(blob["carrier_scale"])

    print("  A 路：exact-key retrieval ＋ typed membership guard")
    print(f"  prereg: BRIDGE_PREREG_retrieval.md   凍結 core={core}（**不重訓**）")
    print(f"  active injection 固定 n={N_ACTIVE}；pool ∈ {POOLS}（+{HOLDOUT} 個 holdout 永不寫入）；hit/miss = 50/50")
    print("  ⚠️ 這是 transfer/safety baseline，**不是** semantic retrieval\n")

    res = {}
    print(f"  {'pool':>5s} {'retr exact':>12s} {'R_abstain':>11s} {'false_ab':>9s} "
          f"{'halluc':>8s} {'E2E|retr':>18s} {'shadow halluc':>14s} {'µs/查':>8s}")
    for pool in POOLS:
        rng = random.Random(70000 + pool)
        # holdout 由**固定 seed** 抽定，三個 pool 共用同一組，確保 miss 查詢可比
        hrng = random.Random(4242)
        holdout = hrng.sample(ALL_DESC, HOLDOUT)
        avail = [d for d in ALL_DESC if d not in holdout]
        desc = rng.sample(avail, pool)
        store = ST.Store()
        for nm, ai in desc:
            store.commit(nm, ai, rng.randint(B.VMIN, B.VMAX))
        assert store.verify() == 0, "store 契約自檢不過"
        absent_pool = list(holdout)   # **只從 holdout 抽 miss**，三個 pool 完全一致

        retr_ok = ab = fab = hal = e2e_ok = e2e_n = shadow_hal = 0
        n_hit = n_miss = 0
        lat_t = 0.0
        for i in range(n):
            hit = (i % 2 == 0)                       # 固定 50/50
            if hit:
                tgt = desc[rng.randrange(len(desc))]
                n_hit += 1
            else:
                if not absent_pool:
                    continue
                tgt = absent_pool[rng.randrange(len(absent_pool))]
                n_miss += 1

            t0 = time.perf_counter()
            z, status = ST.retrieve(store, *tgt)
            lat_t += time.perf_counter() - t0

            # shadow：沒有 guard 的相似度檢索會怎樣（**不得覆寫 guard**）
            bk, _ = ST.shadow_similarity(store, *tgt)
            if not hit and bk is not None:
                shadow_hal += 1                      # 它會回一條「最像的」而不是承認不存在

            if status != "ok":
                if hit:
                    fab += 1                          # 該答卻 abstain
                else:
                    ab += 1                           # 正確 abstain
                continue
            if not hit:
                hal += 1                              # 不該有：absent 卻通過 guard
                continue

            retr_ok += 1
            # 注入 target + 1 個 distractor（n=2；core 沒訓練過 n=1）
            others = [d for d in desc if d != tgt]
            dis = others[rng.randrange(len(others))]
            ep = make_ep(tgt, dis, S.latent_value(z), S.latent_value(store.read(*dis)))
            order = [0, 1]; rng.shuffle(order)
            lat = torch.stack([store.read(*[tgt, dis][k]) for k in order])
            mc = proj(lat.to(DEVICE)).unsqueeze(0)
            ids = torch.tensor(tok(tok.bos_token + S.render_latent(ep)[0],
                                   add_special_tokens=False).input_ids)
            e2e_ok += int(gen(m, tok, ids, loops, mc) == ep.answer)
            e2e_n += 1

        lo, hi = wilson(e2e_ok, e2e_n) if e2e_n else (0, 1)
        print(f"  {pool:>5d} {f'{retr_ok}/{n_hit}':>12s} {f'{ab}/{n_miss}':>11s} "
              f"{fab:>9d} {hal:>8d} "
              f"{e2e_ok/max(e2e_n,1):>9.1%}[{lo:.0%},{hi:.0%}] "
              f"{f'{shadow_hal}/{n_miss}':>14s} {lat_t/n*1e6:>8.1f}")
        res[pool] = {"retr": [retr_ok, n_hit], "abstain": [ab, n_miss],
                     "false_abstain": fab, "halluc": hal,
                     "e2e": [e2e_ok, e2e_n], "shadow_halluc": [shadow_hal, n_miss],
                     "us_per_lookup": lat_t / n * 1e6,
                     "store_bytes": sum(v[1].numel() * 4 + v[0].numel() * 4
                                        for v in store._d.values())}

    print("\n  判讀（prereg §4）：")
    print("    retrieval 與 halluc 應隨 pool **平坦**；不平坦 = store/index 實作問題，非容量曲線")
    print("    halluc 應為 0（absent 在注入前就 abstain）；false_abstain 應為 0")
    print("    shadow = 沒有 guard 時會有多少次「回一條最像的」而不是承認不存在")
    for p in POOLS:
        r = res[p]
        print(f"    pool={p:2d}  store {r['store_bytes']} bytes  "
              f"{r['us_per_lookup']:.1f} µs/查")
    json.dump(res, open(os.path.join(HERE, "results_bridge_retrieval_a.json"), "w"),
              indent=2, ensure_ascii=False)
    print("  -> results_bridge_retrieval_a.json")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 250)
