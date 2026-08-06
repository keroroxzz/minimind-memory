"""H3 的正式判讀 —— 三個 channel 在**同一批 episode** 上的 `n_carrier` 衰減斜率。

**預先登記在 `BRIDGE_PREREG_h3.md`（寫於 `v_only` 落地之前）。本檔只執行。**

為什麼 primary 不是總體正確率：`v_only` 的寫入通道只有 `kv` 的一半，所以
「`v_only` 總體較低」同時相容於 H3 成立與不成立，**不可判**。primary 因此是
**以各 channel 自己的 `n_carrier=1` 為基準的衰減斜率** —— 容量差異被吸收進截距。

    logit P(correct) = α_c + β_c · (n_carrier − 1)
    δ = β_v_only − β_kv

`kv` 在 `n_carrier=1` 是 119/119（完全分離，MLE 發散），所以係數加 **L2 ridge
λ=1.0**，CI 用**配對 bootstrap**（三個 channel 共用同一組重抽索引）。
ridge 對截距也罰 —— 這正是處理分離的那一項；因為三個 channel 受同樣的收縮，
δ 是被**保守地縮向 0**，也就是往「不支持 H3」的方向偏，這對宣稱 SUPPORTED 是安全的。
"""
import argparse
import json
import os
import random
import sys
import time

import numpy as np
import torch

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import bridge_renderer as B
from bridge_delivery import Delivery, greedy, make_item
from bridge_train_core import DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
LAM = 1.0          # ridge，鎖死
NBOOT = 2000       # 鎖死
DELTA_GATE = 0.5   # 鎖死（來源見 prereg）


def fit(x, y, lam=LAM, iters=50):
    """ridge logistic 的 IRLS。回傳 (alpha, beta)。只有兩個參數，收斂很快。"""
    X = np.column_stack([np.ones_like(x, dtype=float), x.astype(float)])
    w = np.zeros(2)
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-X @ w))
        W = np.clip(p * (1 - p), 1e-9, None)
        H = X.T @ (X * W[:, None]) + lam * np.eye(2)
        g = X.T @ (y - p) - lam * w
        step = np.linalg.solve(H, g)
        w += step
        if np.max(np.abs(step)) < 1e-10:
            break
    return w[0], w[1]


@torch.no_grad()
def collect(tags, n, js, seed_base=777):
    """三個 channel 跑**同一批 episode**（同 seed → `make_item` 逐題相同）。"""
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    blob = torch.load(os.path.join(HERE, "bridge_core.pth"), map_location="cpu")
    arch = dict(blob["arch"])
    cfg = MiniMindConfig(vocab_size=blob["vocab"], **arch)
    m = MiniMindForCausalLM(cfg).to(DEVICE).eval()
    m.load_state_dict(blob["model"])
    for p in m.parameters():
        p.requires_grad_(False)

    nl = arch["num_hidden_layers"] * arch["num_loops"]
    dls, loops = {}, None
    for t in tags:
        d = torch.load(os.path.join(HERE, f"bridge_delivery{t}.pth"), map_location="cpu")
        dl = Delivery(B.LATENT_DIM, nl, arch["num_key_value_heads"],
                      arch["hidden_size"] // arch["num_attention_heads"],
                      nfreq=d.get("nfreq", 0), mode=d.get("mode", "kv")).to(DEVICE)
        dl.load_state_dict(d["delivery"])
        dl.eval()
        dls[t] = (dl, d.get("mode", "kv"), d.get("nfreq", 0))
        loops = d["loops"]
        print(f"  {t}: mode={d.get('mode','kv')} nfreq={d.get('nfreq',0)}")

    recs = {}
    for j in js:
        rng = random.Random(seed_base + j)
        rows = []
        for _ in range(n):
            ep, mask, ids, pos, Z, M = make_item(tok, rng, j)
            nc = sum(mask)
            nu = sum(1 for i in ep.ask_idx if mask[i])
            r = {"n_carrier": nc, "n_used": nu}
            for t, (dl, _, _) in dls.items():
                r[t] = int(greedy(m, tok, ids, loops, dl, Z, M, nl) == ep.answer)
            rows.append(r)
        recs[j] = rows
        print(f"  j={j}: {len(rows)} episodes 收集完成")
    return recs, {t: dls[t][1] for t in dls}


def analyse(rows, tags, rng):
    """主分析在**控制住 `n_used`** 之後做（prereg）；配對 bootstrap 共用索引。

    ⚠️ prereg 原本寫「限制在 `n_used=1`」並附註「`j=0` 天然全部 `n_used=1`」。
       那句附註對 `j=0` 正確，但 **`j=1` 的 `n_used` 恆為 2**（鏈用到兩條相異 fact，
       且 `force_used=True` 保證兩條都是載體），所以 `n_used==1` 在 `j=1` 是**空集合**，
       原版在這裡直接 IndexError。

       prereg 的**意圖**是「只用 `n_used` 控制後的配對樣本」（Codex [131] 原話），
       不是字面上的 `=1`。因此改成**取該 `j` 的眾數 `n_used`**（j=0→1、j=1→2），
       控制的性質不變（同一 `j` 內 `n_used` 固定，只讓 `n_carrier` 變動）。
       這是**規格缺陷的修正，不是看到結果後挑樣本** —— `j=0` 的數字完全不受影響。
    """
    if not rows:
        return {"n_used_fixed": None, "n_sub": 0}, {t: [] for t in tags}
    modal = max({r["n_used"] for r in rows},
                key=lambda u: sum(1 for r in rows if r["n_used"] == u))
    sub = [r for r in rows if r["n_used"] == modal]
    x = np.array([r["n_carrier"] - 1 for r in sub], dtype=np.int64)
    ys = {t: np.array([r[t] for r in sub], dtype=float) for t in tags}
    out = {"n_used_fixed": modal, "n_sub": len(sub)}
    for t in tags:
        a, b = fit(x, ys[t])
        out[t] = {"alpha": a, "beta": b,
                  "acc_by_nc": {str(k): [int(ys[t][x == k - 1].sum()),
                                         int((x == k - 1).sum())]
                                for k in sorted({r["n_carrier"] for r in sub})}}
    boots = {t: [] for t in tags}
    n = len(sub)
    for _ in range(NBOOT):
        idx = np.array([rng.randrange(n) for _ in range(n)])   # 三個 channel 同索引
        for t in tags:
            boots[t].append(fit(x[idx], ys[t][idx])[1])
    for t in tags:
        out[t]["beta_ci"] = [float(np.percentile(boots[t], 2.5)),
                             float(np.percentile(boots[t], 97.5))]
    return out, boots


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kv", default="_f8", help="mode=kv 的 tag（基準 channel）")
    ap.add_argument("--v-only", default="_f8v")
    ap.add_argument("--k-only", default="_f8k")
    ap.add_argument("-n", type=int, default=600)
    ap.add_argument("--js", type=int, nargs="+", default=[0, 1])
    ap.add_argument("--seed", type=int, default=31337)
    a = ap.parse_args()

    tags = [t for t in (a.kv, a.v_only, a.k_only)
            if os.path.exists(os.path.join(HERE, f"bridge_delivery{t}.pth"))]
    missing = [t for t in (a.kv, a.v_only, a.k_only) if t not in tags]
    print(f"  預先登記：BRIDGE_PREREG_h3.md   ridge λ={LAM}  bootstrap={NBOOT}  "
          f"δ 門檻={DELTA_GATE}")
    if missing:
        print(f"  ⚠️ 缺 channel：{missing} —— **依 prereg 只登記數字，不下裁決**\n")
    else:
        print()

    t0 = time.time()
    recs, modes = collect(tags, a.n, a.js)
    print(f"  收集耗時 {(time.time()-t0)/60:.1f} min\n")

    res = {}
    for j in a.js:
        rng = random.Random(a.seed + j)
        out, boots = analyse(recs[j], tags, rng)
        print(f"  ==== j={j}   (控制在 n_used={out['n_used_fixed']}，樣本 {out['n_sub']})")
        print(f"    {'channel':>8s} {'mode':>8s} {'beta':>8s} {'95% CI':>18s}   逐 n_carrier")
        for t in tags:
            ci = out[t]["beta_ci"]
            cells = "  ".join(f"{k}:{v[0]}/{v[1]}"
                              for k, v in out[t]["acc_by_nc"].items())
            print(f"    {t:>8s} {modes[t]:>8s} {out[t]['beta']:>8.3f} "
                  f"[{ci[0]:>7.3f},{ci[1]:>7.3f}]   {cells}")
        if a.kv in tags and a.v_only in tags:
            d = np.array(boots[a.v_only]) - np.array(boots[a.kv])
            lo, hi = float(np.percentile(d, 2.5)), float(np.percentile(d, 97.5))
            delta = out[a.v_only]["beta"] - out[a.kv]["beta"]
            out["delta"] = {"point": delta, "ci": [lo, hi]}
            print(f"    δ = β({a.v_only}) − β({a.kv}) = {delta:+.3f}  "
                  f"95% CI [{lo:+.3f}, {hi:+.3f}]")
            # ---- 可判性與裁決，門檻全部來自 prereg ----
            # 可判性門檻要看**該 j 實際存在的最小 n_carrier**。
            # j=1 的 n_used=2，所以根本沒有 n_carrier=1 這一格；
            # 初版寫死 .get("1") 會回傳 None，然後印出「n_carrier=1 只有 0.0%」——
            # 裁決（UNDECIDABLE）碰巧仍正確，但**理由是假的**，會誤導讀者。
            cells = out[a.v_only]["acc_by_nc"]
            nc_min = min(int(k) for k in cells)
            nc1 = cells[str(nc_min)]
            base = nc1[0] / nc1[1] if nc1[1] else 0.0
            if base < 0.95:
                verdict = (f"UNDECIDABLE — {a.v_only} 在該 j 最小的 n_carrier={nc_min} "
                           f"只有 {base:.1%} (<95%)，capacity-insufficient")
            elif missing:
                verdict = "登記數字，不下裁決（k_only 尚未跑，prereg 要求三臂齊備）"
            elif lo <= 0 <= hi:
                verdict = "H3 REFUTED — δ 的 CI 跨 0，斜率無異"
            elif delta >= DELTA_GATE:
                verdict = "H3 SUPPORTED（仍須 k_only 不優於 kv）"
            else:
                verdict = ("H3 PARTIAL — CI 不跨 0 但 δ < 0.5；"
                           "**不是 PASS**，只能寫成「定址成分有貢獻，非主因」")
            print(f"    → {verdict}")
            out["verdict"] = verdict
        res[str(j)] = out
        print()

    json.dump({"prereg": "BRIDGE_PREREG_h3.md", "tags": tags, "missing": missing,
               "modes": modes, "n": a.n, "lam": LAM, "nboot": NBOOT,
               "delta_gate": DELTA_GATE, "seed": a.seed,
               "per_episode": {str(j): recs[j] for j in a.js},
               "fit": res},
              open(os.path.join(HERE, "results_bridge_h3_slope.json"), "w"),
              indent=2, ensure_ascii=False)
    print("  -> results_bridge_h3_slope.json（含逐題結果，可重算）")


if __name__ == "__main__":
    main()
