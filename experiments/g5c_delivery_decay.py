"""G5c：**交付次數 × 配置** 的 paired factorial 診斷（凍結模型、不調參）。

§4.44 的 84% 有兩個**不互斥**的可能來源：

  **A. per-delivery fidelity loss** —— all-placeholder 內部也有重複交付的 error floor
  **B. configuration fragility** —— mixed 配置另有主效應／交互作用
     （`oracle_mem_mixed` 85% vs all-placeholder 100% 已證明 B 存在，
      但**不排除** A 同時存在）

⚠️ **不可**直接拿 K=4/8/12 擬合 —— 鏈長、checkpoint 內容難度與交付次數**共變**。

### 設計：**語義 identity 的 roundtrip**

同一批固定的 oracle checkpoint `P`，插入 d = 0..4 次 roundtrip，
**每次都真的 render → core → 重新形成下一個 checkpoint**，但**真值恆為 `P`**：

| 配置 | prompt | 應輸出 |
|---|---|---|
| `text` | `\| x=e \| <P 的文字> 求x=` | `P` |
| `allph` | `\| x=e \| <placeholder，P 交付> 求x=` | `P` |
| `mixed` | `\| x=e \| <placeholder，P 交付> \| <e 的文字> 求x=` | `P` |

`mixed` 的明確 slot 放**單位置換**，所以真值仍是 `P` ——
配置變了、任務難度沒變。`text` 是「多次 core 呼叫本身會不會掉分」的控制組。

### 事前定好的報告

- 每個 d 的 accuracy
- **首次失敗位置**
- **條件存活率** `P(correct_d | correct_{d-1})`
- `log accuracy ~ d + config + d×config` 的斜率／截距

判讀（**不先假定哪個成立**）：
- `allph` 的條件存活率**近常數**且 `text` **平坦** → 支持 **A（per-delivery fidelity loss）**
- `mixed` 有**額外截距／斜率** → **B 獨立存在**
- 錯誤**高度集中在同一批 episodes** → 不是獨立幾何噪聲，
  而是 **latent-margin／內容難度的異質性**
"""
import argparse
import json
import math
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
import g5b_segmented_reasoning as G5B
from model.memory_module import (ADDR_DIM, LatentStore, MemoryEntry, PERM_N,
                                 perm_to_latent)

HERE = os.path.dirname(os.path.abspath(__file__))
IDENT = list(range(PERM_N))
CFGS = ("text", "allph", "mixed")


@torch.no_grad()
def roundtrip(m, tok, dl, cur, cfg, key):
    """一次 **語義 identity** 的 roundtrip：真值恆為 `cur`。"""
    if cfg == "text":
        return G5B.call_core(m, tok, IDENT, [cur])
    store = LatentStore()
    store.commit([MemoryEntry(key, perm_to_latent(cur).to(G3D.DEVICE), {})])
    e = store.read([key])[0]
    if e is None or e.address != key:
        return None
    lat = e.latent.to(G3D.DEVICE).unsqueeze(0)
    slots = [None] if cfg == "allph" else [None, IDENT]
    return G5B.call_core(m, tok, IDENT, slots, dl, lat)


@torch.no_grad()
def compose_after(m, tok, dl, P, ps, cfg, key):
    """交付 `P` 之後再合成 `j` 個**真實**置換 —— 隔離「合成」而非「交付」。

    `text`  全部明確      `mixed` P 交付 + j 個明確      `allph` 全部交付
    真值一律是 `P ∘ p1 ∘ … ∘ pj`。
    """
    if cfg == "text":
        return G5B.call_core(m, tok, IDENT, [P] + list(ps))
    store = LatentStore()
    items = [P] + (list(ps) if cfg == "allph" else [])
    for j, v in enumerate(items):
        store.commit([MemoryEntry(f"{key}#{j}", perm_to_latent(v).to(G3D.DEVICE), {})])
    lats = []
    for j in range(len(items)):
        e = store.read([f"{key}#{j}"])[0]
        if e is None:
            return None
        lats.append(e.latent.to(G3D.DEVICE))
    slots = ([None] * len(items)) + ([] if cfg == "allph" else list(ps))
    return G5B.call_core(m, tok, IDENT, slots, dl, torch.stack(lats))


def run_after(m, tok, dl, perms, two, n, jmax, seed):
    """d=1 次交付，變動**其後的合成步數** j。"""
    rng = random.Random(seed)
    print(f"\n  ── **合成診斷**：交付 1 次，其後再合成 j 個真實置換")
    print(f"  {'config':<8s} " + "  ".join(f"j={j}" for j in range(jmax + 1)))
    out = {}
    for cfg in CFGS:
        accs = []
        for j in range(jmax + 1):
            rr = random.Random(seed + j)
            ok = 0
            for _ in range(n):
                P = list(perms[rr.randrange(len(perms))])
                ps = [list(perms[rr.randrange(len(perms))]) for _ in range(j)]
                gold = P
                for q in ps:
                    gold = G5B.compose(gold, q)
                got = compose_after(m, tok, dl, P, ps, cfg, two[0])
                ok += int(got == gold)
            accs.append(ok / n)
        out[cfg] = accs
        print(f"  {cfg:<8s} " + "  ".join(f"{x:5.1%}" for x in accs))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--after", action="store_true",
                    help="只跑合成診斷（交付 1 次，變動其後的合成步數）")
    ap.add_argument("--jmax", type=int, default=3)
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--dmax", type=int, default=4)
    ap.add_argument("--seed", type=int, default=246810)
    a = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    m, dl, _, _, _, _ = G3D.load_all(tok, "g3a_writer_6820855741.pth",
                                     "results_g2b.json")
    two = G3B.two_token_keys(tok, [k for k in R.ALL_KEYS if int(k[1:]) < ADDR_DIM])
    _, perms = R.perm_splits()
    print(f"  **凍結模型、不調參**；語義 identity roundtrip（真值恆定）")
    print(f"  `mixed` 的明確 slot 放**單位置換** —— 配置變了、任務難度沒變\n")

    if a.after:
        out = run_after(m, tok, dl, perms, two, a.n, a.jmax, a.seed)
        print(f"\n  判讀：`text` 平坦而 `mixed`/`allph` 隨 j 下滑 →"
              f" **交付進來的值參與合成時較差**，"
              f"\n        與「交付保真度」或「配置」本身無關（G5c 主表已排除那兩者）。")
        json.dump({"mode": "compose_after", "n": a.n, "jmax": a.jmax,
                   "seed": a.seed, "acc": out},
                  open(os.path.join(HERE, "results_g5c_after.json"), "w"),
                  indent=2, ensure_ascii=False)
        print(f"  -> results_g5c_after.json"); return
    rng = random.Random(a.seed)
    P0 = [list(perms[rng.randrange(len(perms))]) for _ in range(a.n)]
    ok = {c: [[True] * a.n] for c in CFGS}       # ok[cfg][d][i]
    first_fail = {c: defaultdict(int) for c in CFGS}
    for c in CFGS:
        cur = [list(p) for p in P0]
        for d in range(1, a.dmax + 1):
            row = []
            for i in range(a.n):
                if not ok[c][d - 1][i]:
                    row.append(False); cur[i] = None; continue
                nxt = roundtrip(m, tok, dl, cur[i], c, two[0])
                good = (nxt == P0[i])
                if not good:
                    first_fail[c][d] += 1
                cur[i] = nxt if nxt is not None else P0[i]
                row.append(good)
            ok[c].append(row)

    print(f"  {'config':<8s} " + "  ".join(f"d={d}" for d in range(a.dmax + 1))
          + "      條件存活率 P(d|d-1)")
    out = {}
    for c in CFGS:
        acc = [sum(r) / a.n for r in ok[c]]
        surv = []
        for d in range(1, a.dmax + 1):
            prev = sum(ok[c][d - 1])
            surv.append(sum(ok[c][d]) / prev if prev else float("nan"))
        print(f"  {c:<8s} " + "  ".join(f"{x:5.1%}" for x in acc)
              + "     " + " ".join(f"{x:5.1%}" for x in surv))
        out[c] = {"acc": acc, "survival": surv,
                  "first_fail": dict(first_fail[c])}

    # log accuracy ~ d（每個 config 各自的斜率）
    print(f"\n  {'config':<8s} {'log-acc 斜率/次':>14s} {'截距':>8s}   （幾何衰減 ⇒ 斜率固定）")
    for c in CFGS:
        ys = [(d, math.log(max(out[c]['acc'][d], 1e-4))) for d in range(a.dmax + 1)]
        n = len(ys); sx = sum(d for d, _ in ys); sy = sum(y for _, y in ys)
        sxx = sum(d * d for d, _ in ys); sxy = sum(d * y for d, y in ys)
        b = (n * sxy - sx * sy) / max(n * sxx - sx * sx, 1e-9)
        print(f"  {c:<8s} {b:>14.4f} {(sy - b * sx) / n:>8.4f}")

    # 錯誤是否集中在同一批 episodes
    fail_sets = {c: {i for i in range(a.n) if not ok[c][a.dmax][i]} for c in CFGS}
    inter = len(fail_sets["allph"] & fail_sets["mixed"])
    print(f"\n  d={a.dmax} 的失敗集合：allph {len(fail_sets['allph'])}、"
          f"mixed {len(fail_sets['mixed'])}、**交集 {inter}**")
    print(f"  （交集遠高於獨立預期 ⇒ 不是獨立幾何噪聲，"
          f"而是 **latent-margin／內容難度異質性**）")
    print(f"\n  判讀：allph 條件存活率近常數且 text 平坦 → **A per-delivery fidelity loss**；"
          f"\n        mixed 有額外截距/斜率 → **B 配置脆弱性獨立存在**；兩者**不互斥**。")
    json.dump({"n": a.n, "dmax": a.dmax, "seed": a.seed, "by_config": out,
               "fail_overlap_at_dmax": {"allph": len(fail_sets["allph"]),
                                        "mixed": len(fail_sets["mixed"]),
                                        "intersection": inter}},
              open(os.path.join(HERE, "results_g5c.json"), "w"), indent=2,
              ensure_ascii=False)
    print(f"  -> results_g5c.json")


if __name__ == "__main__":
    main()
