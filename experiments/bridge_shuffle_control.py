"""兩個 shuffled 負對照 —— 在解釋 `n_carrier=1 → 100%` 之前必須先過這一關。

**預先登記在 `BRIDGE_PREREG_shuffle.md`（在本檔任何結果之前寫下）。**
指標、判讀門檻、樣本數都鎖在那裡，本檔只執行，不重新定義。

`results_bridge_diag_ncarrier_f8.json` 量到 `n_carrier=1 → 100.0%`（n=119）。
這個數字有兩條未排除的捷徑：

1. **內容沒被讀** —— core 從模板／位置猜，latent 只是裝飾。
2. **綁定沒被讀** —— 多載體時 core 不需要知道哪條 latent 屬於哪一格。

⚠️ 已知削弱，先寫下：`force_used=True` 使得 `j=0` 且 `n_carrier=1` 時，
   那唯一的載體**必然就是被問的那格**，所以**綁定是平凡的**。
   `n_carrier=1` 最多只能宣稱「無需綁定的內容輸送」。綁定由對照 B 測。

### 反事實評分（這是本檔的核心手法）

每題同時對兩個目標評分：

- `orig` —— 原本的答案
- `cf`   —— 用**這次實際送進去的值**重算 `x ← |x − v|` 得到的答案

只看「shuffle 之後掉下來」是弱的：那與「交付整個壞掉」無法區分。
`cf` 高才證明**送什麼進去就答什麼**，也就是內容真的被讀。

鏈是從 `ep.query` 字串反解的 —— 它唯一的正確性保證是那個硬 assert：
用**真值**重算必須逐題等於 `ep.answer`。失敗就中止，不得繼續。
"""
import argparse
import json
import os
import random
import sys
import time
from collections import defaultdict

import torch

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import bridge_renderer as B
from bridge_delivery import SPAN, Delivery, greedy, make_item, mk_fn
from bridge_train_core import DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
EPS = (0, 1, 2, 5)             # 預先登記鎖死，不得事後新增


# ---------------------------------------------------------------- 鏈的反解
def chain_idx(ep):
    """從 query 字串反解出**有序**的 fact index 鏈。

    `Episode` 只存 `ask_idx = sorted(set(idx))`，順序已經遺失，但 `|x−v|`
    非交換，反事實一定要順序。query 的文法是自己生成的，可逆。
    """
    q = ep.query
    assert q.endswith(" 是 多少"), q
    body = q[: -len(" 是 多少")]
    key = {(nm, ai): i for i, (nm, ai, _) in enumerate(ep.facts)}
    out = []
    for part in body.split(" 差 "):
        nm, at = part.split()
        out.append(key[(nm, B.ATTRS.index(at))])
    return out


def answer_from(ep, vals):
    """給定每個 fact 的**有效值**，重算答案字串。`vals` 是 list，長度 = fact 數。"""
    idx = chain_idx(ep)
    cur = vals[idx[0]]
    for nxt in idx[1:]:
        cur = abs(cur - vals[nxt])
    return B.val_str(cur)


def true_vals(ep):
    return [v for _, _, v in ep.facts]


# ---------------------------------------------------------------- 兩個對照
def content_shuffle(rng, ep, mask):
    """對照 A：位置固定，只把載體格的**值**換成外來值。

    屬性 one-hot **故意保持與文字一致** —— 否則失敗可以被歸因成
    「latent 自相矛盾所以 core 拒答」，那就測不到內容輸送本身。
    """
    base = true_vals(ep)
    car = [i for i, mk in enumerate(mask) if mk]
    for _ in range(200):
        vals = list(base)
        for i in car:
            vals[i] = rng.randint(B.VMIN, B.VMAX)
        # 維持生成器的不變量：全部值互異（避免鏈中途歸零而退化）
        if len(set(vals)) == len(vals) and all(vals[i] != base[i] for i in car):
            # latent 的屬性維持原 fact 的屬性
            lat = [B.fact_latent(vals[i], ep.facts[i][1]) for i in car]
            return vals, lat
    raise RuntimeError("content_shuffle 重抽 200 次仍無法滿足值互異")


def position_shuffle(rng, ep, mask):
    """對照 B：內容集合完全不變，只把載體 latent 在自己的載體位置之間**錯排**。

    `n_carrier>=2` 才有定義。整條 latent（含屬性維）一起搬 —— 這才是
    「同一份內容擺到別的合法 slot」；屬性維若留在原地，就變成半個對照。
    """
    car = [i for i, mk in enumerate(mask) if mk]
    assert len(car) >= 2
    for _ in range(200):
        perm = list(range(len(car)))
        rng.shuffle(perm)
        if all(perm[t] != t for t in range(len(car))):    # derangement
            break
    else:
        raise RuntimeError("錯排抽不出來")
    base = true_vals(ep)
    vals = list(base)
    lat = []
    for t, i in enumerate(car):
        src = car[perm[t]]
        vals[i] = base[src]                      # 這一格的有效值來自別條記憶
        lat.append(B.fact_latent(base[src], ep.facts[src][1]))
    return vals, lat


# ---------------------------------------------------------------- 三個臂
@torch.no_grad()
def run_oracle(m, tok, ep, mask, ids, pos, vals, loops):
    """oracle inline（零參數）：用**實際送進去的值**的 token embedding 覆寫載體格。"""
    ep2 = B.Episode([(nm, ai, vals[i]) for i, (nm, ai, _) in enumerate(ep.facts)],
                    ep.query, ep.answer, ep.ask_idx, ep.j)
    src = torch.tensor(tok(tok.bos_token + B.render(ep2, None)[0],
                           add_special_tokens=False).input_ids)
    assert len(src) == len(ids), "L0(替換值) 與 carrier 的 token 長度必須相同"
    e = m.model.embed_tokens(ids.to(DEVICE)).clone()
    p = pos.to(DEVICE)
    e[p] = m.model.embed_tokens(src.to(DEVICE))[p]
    o = m(ids.unsqueeze(0).to(DEVICE), inputs_embeds=e.unsqueeze(0),
          use_cache=True, num_loops=loops)
    pkv, g = o.past_key_values, []
    for _ in range(4):
        nx = o.logits[:, -1].argmax(-1, keepdim=True)
        if nx.item() == tok.eos_token_id:
            break
        g.append(nx.item())
        o = m(nx, past_key_values=pkv, use_cache=True, num_loops=loops)
        pkv = o.past_key_values
    return tok.decode(g, skip_special_tokens=True).strip()


def stamp(ids, pos, lat):
    """把（可能被 shuffle 過的）latent 重新貼回載體位置。每個值 2 token → 重複 SPAN 次。"""
    Z = torch.zeros(len(ids), B.LATENT_DIM)
    M = torch.zeros(len(ids), 1)
    Z[pos] = torch.tensor(lat, dtype=torch.float32).repeat_interleave(SPAN, dim=0)
    M[pos] = 1.0
    return Z, M


# ---------------------------------------------------------------- 指標
def parse(s):
    """`"3 4"` → 34。不是恰好兩個個位數就是 `parse_fail`（回傳 None）。"""
    p = s.split()
    if len(p) == 2 and all(len(x) == 1 and x.isdigit() for x in p):
        return int(p[0]) * 10 + int(p[1])
    return None


def metrics(recs, key):
    """`recs` 是 (pred_str, orig_str, cf_str, n_carrier)；`key` 決定對哪個目標評分。

    **`exact_nc`（非碰撞子集）是判「位置捷徑」時唯一可用的欄位。**
    `j>=1` 的 `|x−v|` 鏈可能讓 `cf` 與 `orig` 剛好相同（例如交換後鏈值撞在一起）；
    這種題目答對 `cf` 必然同時答對 `orig`，會把 `orig` 這欄灌水，看起來像捷徑。
    所以 `orig` 一律同時報整體與**排除 `cf == orig`** 之後的版本。
    （這是在 n=8 smoke 看到 j=1 oracle 的 `orig` 是 50% 才發現的量測缺陷，
    在跑正式 n 之前修掉，不是看到正式結果後才改判讀。）
    """
    n = len(recs)
    if n == 0:
        return {"n": 0}
    tgt = {"orig": 1, "cf": 2}[key]
    errs, ex, tens, ones, fail = [], 0, 0, 0, 0
    nc_ok = nc_n = 0
    per_c = defaultdict(lambda: [0, 0])
    for r in recs:
        t = parse(r[tgt])
        assert t is not None, f"目標本身無法解析：{r[tgt]!r}"
        p = parse(r[0])
        ok = int(r[0] == r[tgt])
        ex += ok
        if r[1] != r[2]:                      # cf 與 orig 不同的題目才算得準
            nc_n += 1
            nc_ok += ok
        per_c[r[3]][0] += ok
        per_c[r[3]][1] += 1
        if p is None:
            fail += 1
            continue
        errs.append(p - t)
        tens += int(p // 10 == t // 10)
        ones += int(p % 10 == t % 10)
    out = {"n": n, "exact": ex / n, "parse_fail": fail / n,
           "n_noncollide": nc_n, "exact_nc": (nc_ok / nc_n) if nc_n else None,
           "by_n_carrier": {str(k): per_c[k] for k in sorted(per_c)}}
    if errs:
        a = sorted(abs(e) for e in errs)
        q = lambda f: a[min(len(a) - 1, int(f * len(a)))]
        out.update({
            "n_parsed": len(errs),
            "MAE": sum(a) / len(a),
            "RMSE": (sum(e * e for e in errs) / len(errs)) ** 0.5,
            "signed_bias": sum(errs) / len(errs),
            "P_within": {str(e): sum(1 for x in a if x <= e) / len(a) for e in EPS},
            "P50": q(0.50), "P90": q(0.90), "P99": q(0.99),
            "tens": tens / len(errs), "ones": ones / len(errs),
        })
    return out


def wilson(k, n):
    """95% Wilson 區間 —— 判讀門檻要求「CI 不重疊」，所以必須逐格報。"""
    if n == 0:
        return (0.0, 1.0)
    z, p = 1.96, k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, c - h), min(1.0, c + h))


# ---------------------------------------------------------------- 主程式
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="_f8", help="受測的 delivery 權重後綴")
    ap.add_argument("-n", type=int, default=250, help="每個 (j, control) 的題數")
    ap.add_argument("--js", type=int, nargs="+", default=[0, 1])
    ap.add_argument("--seed", type=int, default=20260805)
    ap.add_argument("--throttle", type=float, default=0.8)
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    blob = torch.load(os.path.join(HERE, "bridge_core.pth"), map_location="cpu")
    arch = dict(blob["arch"])
    cfg = MiniMindConfig(vocab_size=blob["vocab"], **arch)
    m = MiniMindForCausalLM(cfg).to(DEVICE).eval()
    m.load_state_dict(blob["model"])
    for p in m.parameters():
        p.requires_grad_(False)

    d = torch.load(os.path.join(HERE, f"bridge_delivery{a.tag}.pth"), map_location="cpu")
    loops, nl = d["loops"], arch["num_hidden_layers"] * arch["num_loops"]
    dl = Delivery(B.LATENT_DIM, nl, arch["num_key_value_heads"],
                  arch["hidden_size"] // arch["num_attention_heads"],
                  nfreq=d.get("nfreq", 0), mode=d.get("mode", "kv")).to(DEVICE)
    dl.load_state_dict(d["delivery"])
    dl.eval()

    print(f"  受測 delivery: bridge_delivery{a.tag}.pth  "
          f"nfreq={d.get('nfreq', 0)}  mode={d.get('mode', 'kv')}  num_loops={loops}")
    print(f"  預先登記：BRIDGE_PREREG_shuffle.md（指標／門檻／n 都鎖在那裡）")
    print(f"  seed={a.seed}  n={a.n}/格  j={a.js}   eps={EPS}\n")

    res, t0 = {}, time.time()
    # ⚠️ 不可用 `hash(str)` 當 seed 偏移 —— Python 對 str 的 hash 每個行程都不同
    #    （PYTHONHASHSEED 隨機化），那會讓「同一個 seed」根本不可重現。
    CTRL_OFF = {"intact": 0, "content": 1, "position": 2}
    for j in a.js:
        for ctrl in ("intact", "content", "position"):
            rng = random.Random(a.seed + 1000 * j + 7 * CTRL_OFF[ctrl])
            seen, recs = set(), {k: [] for k in ("nodeliver", "oracle", "learned")}
            while len(seen) < a.n:
                ep, mask, ids, pos, Z, M = make_item(tok, rng, j)
                if ep.eid in seen:
                    continue                       # 去重：不得重複題目
                if ctrl == "position" and sum(mask) < 2:
                    continue                       # 錯排需要 >=2 條載體
                seen.add(ep.eid)

                # 硬 assert：鏈的反解必須重現原答案，否則反事實全部不可信
                assert answer_from(ep, true_vals(ep)) == ep.answer, \
                    f"鏈反解失敗 j={j} q={ep.query!r}"

                if ctrl == "intact":
                    vals = true_vals(ep)
                    lat = B.episode_latents(ep, mask)
                elif ctrl == "content":
                    vals, lat = content_shuffle(rng, ep, mask)
                else:
                    vals, lat = position_shuffle(rng, ep, mask)
                cf = answer_from(ep, vals)
                Zs, Ms = stamp(ids, pos, lat)

                s0 = time.time()
                got = {
                    "nodeliver": greedy(m, tok, ids, loops),
                    "oracle": run_oracle(m, tok, ep, mask, ids, pos, vals, loops),
                    "learned": greedy(m, tok, ids, loops, dl, Zs, Ms, nl),
                }
                if a.throttle < 1.0 and DEVICE == "cuda":
                    torch.cuda.synchronize()
                    time.sleep((time.time() - s0) * (1 / a.throttle - 1))
                for k, g in got.items():
                    recs[k].append((g, ep.answer, cf, sum(mask)))

            print(f"  ==== j={j}  control={ctrl}   ({(time.time()-t0)/60:.1f} min)")
            print(f"    {'arm':>10s} {'exact|orig':>12s} {'exact|cf':>10s} "
                  f"{'MAE|cf':>8s} {'P<=2|cf':>9s} {'fail':>6s}")
            for k in ("nodeliver", "oracle", "learned"):
                mo, mc = metrics(recs[k], "orig"), metrics(recs[k], "cf")
                res[f"j{j}|{ctrl}|{k}"] = {"orig": mo, "cf": mc}
                lo, hi = wilson(round(mc["exact"] * mc["n"]), mc["n"])
                print(f"    {k:>10s} {mo['exact']:>11.1%} {mc['exact']:>9.1%} "
                      f"{mc.get('MAE', float('nan')):>8.2f} "
                      f"{mc.get('P_within', {}).get('2', float('nan')):>8.1%} "
                      f"{mc['parse_fail']:>5.1%}   cf 95%CI [{lo:.1%}, {hi:.1%}]")
            print()

    # ---- 判讀（門檻鎖在 prereg，這裡只是照著印，不重新定義）------------------
    print("  ---- 依 BRIDGE_PREREG_shuffle.md §3 判讀")
    for j in a.js:
        base = res.get(f"j{j}|intact|learned")
        cs = res.get(f"j{j}|content|learned")
        ps = res.get(f"j{j}|position|learned")
        nd = res.get(f"j{j}|content|nodeliver")
        if not (base and cs and nd):
            continue
        pc = lambda c: (f"{c['orig']['exact_nc']:.1%}"
                        if c["orig"]["exact_nc"] is not None else "n/a")
        gap = base["orig"]["exact"] - cs["cf"]["exact"]
        print(f"    j={j}  內容 shuffle：cf {cs['cf']['exact']:.1%} vs intact "
              f"{base['orig']['exact']:.1%}   差 {gap*100:+.1f}pp  "
              + ("→ **內容真被讀**" if gap <= 0.05 else "→ **未達 5pp 門檻**"))
        # ⚠️ 這裡一律**用數字算**，不得印固定標籤 —— 初版把「顯著高 → …」寫死成
        #    括號註解，結果數字顯示的是相反方向，讀起來像裁決。
        def cmp_nd(c, base, what):
            a, b = c["orig"]["exact_nc"], base["orig"]["exact_nc"]
            if a is None or b is None:
                return "n/a"
            la, ha = wilson(round(a * c["orig"]["n_noncollide"]), c["orig"]["n_noncollide"])
            lb, hb = wilson(round(b * base["orig"]["n_noncollide"]), base["orig"]["n_noncollide"])
            return (f"**{what}：是**（CI 不重疊）" if la > hb else
                    f"{what}：否（{a:.1%} 未顯著高於 nodeliver {b:.1%}）")

        print(f"          orig(非碰撞) {pc(cs)} vs nodeliver {pc(nd)}"
              f"  [n={cs['orig']['n_noncollide']}]  → " + cmp_nd(cs, nd, "模板/位置洩漏"))
        if ps:
            nd2 = res[f"j{j}|position|nodeliver"]
            print(f"    j={j}  位置 shuffle：cf {ps['cf']['exact']:.1%}   "
                  f"orig(非碰撞) {pc(ps)} vs nodeliver {pc(nd2)}"
                  f"  [n={ps['orig']['n_noncollide']}]  → " + cmp_nd(ps, nd2, "orig 高於地板"))
            # 第四種情況：cf 與 orig **都中等且相近** = 內容有送到、但綁定不存在
            #（模型在幾條載體之間近似均勻亂挑）。這不是洩漏，content shuffle 可鑑別。
            a, b = ps["orig"]["exact_nc"], ps["cf"]["exact_nc"]
            if a and b and min(a, b) > 0.05 and abs(a - b) < 0.10:
                print(f"          ⚠️ orig({a:.1%}) ≈ cf({b:.1%}) 且都遠高於地板 → "
                      "**內容有送到但位置綁定不存在**（在載體之間亂挑），非洩漏")

    out = a.out or f"results_bridge_shuffle{a.tag}.json"
    json.dump({"prereg": "BRIDGE_PREREG_shuffle.md", "tag": a.tag, "n": a.n,
               "seed": a.seed, "eps": list(EPS), "loops": loops,
               "nfreq": d.get("nfreq", 0), "mode": d.get("mode", "kv"),
               "cells": res}, open(os.path.join(HERE, out), "w"),
              indent=2, ensure_ascii=False)
    print(f"\n  -> {out}")


if __name__ == "__main__":
    main()
