"""G4b 的 learned reference resolver —— 階梯第 2/3 階。

    階梯   oracle（§4.40，9 格全 100%）→ **probe / small overfit** → **formal**

### 事前鎖死的東西（看 test 之前）

- **輸入位置固定**：凍結 core 在 **reference view 最後一個 token** 的**最終層** hidden。
  **不得**看結果後再挑 layer 或 token（Codex）。
- **probe 只是 representation / small-overfit gate**，
  **不可**與正式結果累加成獨立證據 —— 正式 resolver 本質上也是
  `hidden → address` 的線性打分，兩者不是獨立的。
- **temporal 軸不得再混 identity 泛化**：primary 用 resolver 訓練已覆蓋的
  closed-world entity ID，只把**排列、距離、filler 數**留作泛化軸。
  （若 test 的 entity address 是未見的隨機正交基底，線性 Q **不可能**憑空知道映射。）
- **selective policy 事前鎖死**：在 cal 上選使 `wrong-existing` 的
  **單側 95% CP 上界 ≤ 5%** 的 threshold，再**最小化 abstain**；
  **test 只跑一次**。

### 一定要並列的非學習 baseline

**`last-written pointer`**（直接取 stream 裡最後一次寫入）—— 預期 100%。
所以 learned 通過**只能**宣稱「**凍結的 hidden 支援學到 recency 關係**」，
**不能**宣稱這個規則沒辦法由 controller 直接實作。
"""
import argparse
import json
import os
import random
import sys
from collections import defaultdict

import torch
import torch.nn as nn
import torch.nn.functional as F

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import g1_renderer as R
import g3a_train as G3
import g3b_closure as G3B
import g3d_scale_audit as G3D
from g2c_cal_v2 import cp_upper, wilson
from g3a_train import precompute_events
from model.memory_module import ADDR_DIM, address_vector

HERE = os.path.dirname(os.path.abspath(__file__))


class Resolver(nn.Module):
    """`hidden → address 空間` 的線性打分。**address 本身仍是 deterministic 的。**"""

    def __init__(self, in_dim, addr_dim=ADDR_DIM):
        super().__init__()
        self.q = nn.Linear(in_dim, addr_dim)
        self.log_temp = nn.Parameter(torch.zeros(()))

    def forward(self, h, addrs):
        q = F.normalize(self.q(h), dim=-1)
        a = F.normalize(addrs, dim=-1)
        return torch.einsum("bd,bnd->bn", q, a) * self.log_temp.exp()


@torch.no_grad()
def ref_hidden(m, tok, streams, bs=32):
    """**事前固定**：reference view 最後一個 token 的最終層 hidden。"""
    out, by_len = [None] * len(streams), defaultdict(list)
    enc = [tok(tok.bos_token + R.render_reference_view(s), add_special_tokens=False
               ).input_ids for s in streams]
    for i, v in enumerate(enc):
        by_len[len(v)].append(i)
    for _, idxs in by_len.items():
        for c in range(0, len(idxs), bs):
            ch = idxs[c:c + bs]
            ids = torch.tensor([enc[i] for i in ch]).to(G3D.DEVICE)
            h, _, _, _ = m.model(ids, num_loops=G3D.ARCH["num_loops"])
            for j, i in enumerate(ch):
                out[i] = h[j, -1].clone()
    return torch.stack(out)


def build(rng, keys, perms, n, entities, fillers, k=1):
    eps = []
    for _ in range(n):
        ne = entities[rng.randrange(len(entities))]
        nf = fillers[rng.randrange(len(fillers))]
        eps.append(R.make_reference_episode(rng, keys, perms, ne, nf, k) + (ne, nf))
    return eps


def batchify(eps, H, idxs, max_e):
    h = H[idxs]
    addrs = torch.zeros(len(idxs), max_e, ADDR_DIM, device=h.device)
    mask = torch.zeros(len(idxs), max_e, dtype=torch.bool, device=h.device)
    tgt = torch.zeros(len(idxs), dtype=torch.long, device=h.device)
    for r, i in enumerate(idxs):
        _, writes, target, _, _, _ = eps[i]
        for c, (kx, _) in enumerate(writes):
            addrs[r, c] = address_vector(kx).to(h.device); mask[r, c] = True
            if kx == target:
                tgt[r] = c
    return h, addrs, mask, tgt


def evaluate(res, eps, H, idxs, max_e, thr=None):
    h, addrs, mask, tgt = batchify(eps, H, idxs, max_e)
    with torch.no_grad():
        lg = res(h, addrs).masked_fill(~mask, float("-inf"))
        p = lg.softmax(-1)
        conf, pred = p.max(-1)
    ok = (pred == tgt)
    if thr is None:
        return {"exact": ok.float().mean().item(), "n": len(idxs),
                "wrong": (~ok).sum().item()}
    keep = conf > thr
    return {"exact": ok.float().mean().item(), "n": len(idxs),
            "coverage": keep.float().mean().item(),
            "abstain": (~keep).float().mean().item(),
            "wrong_existing": int((keep & ~ok).sum()), "kept": int(keep.sum())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="probe", choices=["probe", "formal"])
    ap.add_argument("--steps", type=int, default=1500)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--n-train", type=int, default=4000)
    ap.add_argument("--n-cal", type=int, default=600)
    ap.add_argument("--n-test", type=int, default=600)
    ap.add_argument("--seed", type=int, default=808)
    ap.add_argument("--risk-cap", type=float, default=0.05)
    a = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    m, dl, writer, _, _, _ = G3D.load_all(tok, "g3a_writer_6820855741.pth",
                                          "results_g2b.json")
    addressable = [k for k in R.ALL_KEYS if int(k[1:]) < ADDR_DIM]
    two = G3B.two_token_keys(tok, addressable)
    _, perms = R.perm_splits()

    # temporal 軸：**entity ID 全程共用**（closed-world），只把排列/距離/filler 當泛化軸
    TR_E, TR_F = [2, 3, 5], [0, 2, 5]
    TE_E, TE_F = ([2, 3, 5], [0, 2, 5]) if a.stage == "probe" else ([3, 4, 6], [1, 3, 7])
    rng = random.Random(a.seed)
    tr = build(rng, two, perms, a.n_train, TR_E, TR_F)
    cal = build(rng, two, perms, a.n_cal, TE_E, TE_F)
    te = build(rng, two, perms, a.n_test, TE_E, TE_F)
    max_e = max(max(TR_E), max(TE_E))

    print(f"  **凍結 core**；resolver 只有 `Linear({G3D.BACKBONE['hidden_size']}→{ADDR_DIM})`"
          f" + temperature")
    print(f"  輸入 = reference view **最後一個 token** 的**最終層** hidden（事前固定）")
    print(f"  entity ID 全程共用（closed-world）；泛化軸 = 排列 / 距離 / filler 數")
    print(f"  train entities{TR_E} fillers{TR_F}  →  cal/test entities{TE_E} fillers{TE_F}")

    # ---- 非學習 baseline：直接取「最後一次寫入」----
    base = sum(1 for e in te if e[1][-1][0] == e[2]) / len(te)
    print(f"\n  **非學習 baseline（`last-written pointer`）= {base:.1%}** ——"
          f"\n  learned 通過只能宣稱「凍結 hidden 支援學到 recency 關係」，"
          f"\n  **不能**宣稱這個規則無法由 controller 直接實作。\n")

    Htr, Hcal, Hte = (ref_hidden(m, tok, [e[0] for e in x]) for x in (tr, cal, te))
    res = Resolver(G3D.BACKBONE["hidden_size"]).to(G3D.DEVICE)
    opt = torch.optim.AdamW(res.parameters(), lr=a.lr, weight_decay=0.01)
    g = torch.Generator().manual_seed(a.seed)
    for step in range(1, a.steps + 1):
        idx = torch.randint(len(tr), (128,), generator=g).tolist()
        h, addrs, mask, tgt = batchify(tr, Htr, idx, max_e)
        lg = res(h, addrs).masked_fill(~mask, float("-inf"))
        loss = F.cross_entropy(lg, tgt)          # **未加權 CE**（Codex）
        opt.zero_grad(set_to_none=True); loss.backward(); opt.step()
        if step % 300 == 0:
            print(f"  step {step:5d} loss={loss.item():.4f}", flush=True)

    tr_r = evaluate(res, tr, Htr, list(range(len(tr))), max_e)
    te_r = evaluate(res, te, Hte, list(range(len(te))), max_e)
    print(f"\n  raw resolver exact：train {tr_r['exact']:.1%} / **test {te_r['exact']:.1%}**"
          f" (n={te_r['n']})")

    if a.stage == "probe":
        print(f"\n  ⚠️ 這是 **representation / small-overfit gate**，"
              f"**不可**與正式結果累加成獨立證據。")
        print(f"  判讀：train 都學不起來 → **表示問題**（hidden 沒編碼 recency），"
              f"不是學習問題。")
        json.dump({"stage": "probe", "train": tr_r, "test": te_r, "baseline": base},
                  open(os.path.join(HERE, "results_g4b_probe.json"), "w"), indent=2)
        print(f"  -> results_g4b_probe.json"); return

    # ---- 事前鎖死的 selective policy：cal 選 threshold，test 只跑一次 ----
    h, addrs, mask, tgt = batchify(cal, Hcal, list(range(len(cal))), max_e)
    with torch.no_grad():
        p = res(h, addrs).masked_fill(~mask, float("-inf")).softmax(-1)
        conf, pred = p.max(-1)
    ok = (pred == tgt)
    cand = sorted(set(conf.tolist()))
    feas = []
    for t in [0.0] + [(x + y) / 2 for x, y in zip(cand[:-1], cand[1:])] + [1.0]:
        keep = conf > t
        w, n = int((keep & ~ok).sum()), int(keep.sum())
        if n and cp_upper(w, n) <= a.risk_cap:
            feas.append(((~keep).float().mean().item(), t, cp_upper(w, n)))
    if not feas:
        print(f"\n  ❌ cal 上沒有 threshold 能把 wrong-existing 的 95% 上界壓到 "
              f"≤ {a.risk_cap:.0%} —— 依預先登記判 **FAIL**，不放寬。")
        return
    best = min(f for f, _, _ in feas)
    thr = max(t for f, t, _ in feas if f <= best + 1e-12)
    print(f"\n  cal 選出 threshold = {thr:.4f}（wrong-existing 上界 ≤ {a.risk_cap:.0%} 下"
          f" abstain 最小 = {best:.1%}）")

    r = evaluate(res, te, Hte, list(range(len(te))), max_e, thr=thr)
    ub = cp_upper(r["wrong_existing"], max(r["kept"], 1))
    print(f"\n  **test（只跑一次，n={r['n']}）**")
    print(f"    raw resolver exact        {r['exact']:6.1%}")
    print(f"    coverage（有作答）         {r['coverage']:6.1%}")
    print(f"    abstain                   {r['abstain']:6.1%}")
    print(f"    **guarded wrong-existing** {r['wrong_existing']}/{r['kept']} = "
          f"{r['wrong_existing']/max(r['kept'],1):.1%}   單側 95% 上界 **{ub:.2%}**")
    ok_ = ub <= a.risk_cap
    print(f"\n  裁決：**{'PASS' if ok_ else 'FAIL'}**（gate：wrong-existing 上界 ≤ "
          f"{a.risk_cap:.0%}）")
    print(f"  ⚠️ exact-membership guard **擋不住**指錯到已存在的 entity，"
          f"所以這個 selective policy 是 G4b 的**必要子結果**，不是附加。"
          f"\n     只稱 **closed-world temporal binding**，不稱完整安全閉環。")
    json.dump({"stage": "formal", "threshold": thr, "test": r, "halluc_ub95": ub,
               "baseline_last_written": base, "train": tr_r,
               "verdict": "PASS" if ok_ else "FAIL"},
              open(os.path.join(HERE, "results_g4b_formal.json"), "w"), indent=2)
    print(f"  -> results_g4b_formal.json")


if __name__ == "__main__":
    main()
