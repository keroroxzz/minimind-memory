"""binding loss 的機制量測 —— **純量測，不做任何 scale 介入**。

`[135]` 量到 `||ΔK||/||K_nat||` 中位數 **7.0×**，據此提出「native 定址被淹沒」。
Codex [135] 的要求（照做，不打折）：

> 不要只量一個全局比值就寫機制。每個 slot／layer／position 同報
> `||f(z)||`、`||K_nat||`、`||K'||`、`cos(K',K_nat)`、carrier 間 K' 相似度，
> 以及 **answer query 對各 carrier 的 attention mass**；
> 與**被選中的 slot**、`orig/cf`、`1/n` 偏差配對。
> **若比值不預測「選中誰／錯綁」，撤回這條解釋。**

所以本檔的核心不是範數，而是**預測力**：

    attention mass 最大的那個 carrier，是不是模型實際講出來的那個值？

- 若 attention 已經是均勻 `1/n`，且**不預測**選中誰 → 「淹沒」只是必要條件，
  下一步應該是**顯式 slot identity／binding channel**（Codex [135] 結論）。
- 若 attention 顯著預測選中誰 → 選擇發生在 attention 層，可沿這條往下修。
- 若兩者都不成立 → **撤回「native address 被淹沒」這條解釋**。

⚠️ 這是 **post-hoc 診斷**，不是預先登記的 primary。
⚠️ **不改 core、不重訓、不做 scale 介入** —— G7a 已示範擾動已學工作點會出現倒 U，
   相關性不得重新包裝成因果。

### 怎麼拿到 attention weight

core 走 SDPA，不回傳權重。這裡**暫時包一層 `F.scaled_dot_product_attention`**
把每次呼叫的 `(q, k)` 記下來，再自己算 softmax —— 只在診斷期間生效，
用 `finally` 還原，不動模型程式碼。
"""
import json
import math
import os
import random
import sys
from collections import defaultdict

import torch
import torch.nn.functional as F

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import bridge_renderer as B
from bridge_delivery import SPAN, Delivery, make_item
from bridge_train_core import DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
_ORIG_SDPA = F.scaled_dot_product_attention


class Capture:
    """包住 SDPA 記下 (q, k)。只記 prompt 那一次前向（seq_len > 1）。"""

    def __init__(self):
        self.qk = []
        self.on = False

    def __enter__(self):
        cap = self

        def patched(q, k, v, attn_mask=None, dropout_p=0.0, is_causal=False, **kw):
            if cap.on and q.shape[2] > 1:
                cap.qk.append((q.detach(), k.detach()))
            return _ORIG_SDPA(q, k, v, attn_mask=attn_mask, dropout_p=dropout_p,
                              is_causal=is_causal, **kw)

        F.scaled_dot_product_attention = patched
        torch.nn.functional.scaled_dot_product_attention = patched
        return self

    def __exit__(self, *a):
        F.scaled_dot_product_attention = _ORIG_SDPA
        torch.nn.functional.scaled_dot_product_attention = _ORIG_SDPA


def wilson(k, n):
    if n == 0:
        return (0.0, 1.0)
    z, p = 1.96, k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, c - h), min(1.0, c + h))


def attn_mass(qk, carrier_pos, is_causal=True):
    """最後一個 prompt 位置（產生第一個答案 token 的那個 query）對各 carrier 的 attention mass。

    回傳 `(平均, 逐 slot)`：平均是對所有 (loop, layer) 與 head 平均；
    逐 slot 保留每個 (loop, layer) 的分布 —— **平均可能掩蓋某個真的在做選擇的 slot**，
    Codex [135] 要求逐 slot 報，這裡照做。
    """
    per_slot = []
    tot = None
    for q, k in qk:
        hd = q.shape[-1]
        # ⚠️ 必須保留 query 的長度維：`q[0,:,-1,:]` 是 2-D (H,hd)，與 3-D 的
        #    (H,hd,T_k) 相乘會被 broadcast 成 (H,H,T_k) —— 形狀看起來合法，
        #    再用 token 位置去索引就會撞到 head 維而 CUDA assert。用 -1: 保住維度。
        s = (q[0, :, -1:, :].float() @ k[0].float().transpose(-2, -1)).squeeze(1)
        s = s / math.sqrt(hd)
        assert s.dim() == 2 and s.shape[0] == q.shape[1], f"attention 形狀錯了 {s.shape}"
        p = s.softmax(-1).mean(0)                     # 對 head 平均 -> (T_k,)
        assert p.dim() == 1, p.shape
        masses = [float(p[torch.as_tensor(pp, device=p.device)].sum())
                  for pp in carrier_pos]
        per_slot.append(masses)
        tot = masses if tot is None else [a + b for a, b in zip(tot, masses)]
    return [t / len(qk) for t in tot], per_slot


@torch.no_grad()
def main(tag="_f8", n=300):
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    blob = torch.load(os.path.join(HERE, "bridge_core.pth"), map_location="cpu")
    arch = dict(blob["arch"])
    cfg = MiniMindConfig(vocab_size=blob["vocab"], **arch)
    m = MiniMindForCausalLM(cfg).to(DEVICE).eval()
    m.load_state_dict(blob["model"])

    d = torch.load(os.path.join(HERE, f"bridge_delivery{tag}.pth"), map_location="cpu")
    loops, nl = d["loops"], arch["num_hidden_layers"] * arch["num_loops"]
    dl = Delivery(B.LATENT_DIM, nl, arch["num_key_value_heads"],
                  arch["hidden_size"] // arch["num_attention_heads"],
                  nfreq=d.get("nfreq", 0), mode=d.get("mode", "kv")).to(DEVICE)
    dl.load_state_dict(d["delivery"])
    dl.eval()
    print(f"  {tag}  mode={d.get('mode','kv')}  num_loops={loops}   n={n}   j=0")
    print("  ⚠️ post-hoc 診斷；純量測，不改 core、不重訓、**不做 scale 介入**\n")

    norms = defaultdict(list)
    rows = []
    rng = random.Random(515151)
    tried = 0
    while len(rows) < n and tried < n * 6:
        tried += 1
        ep, mask, ids, pos, Z, M = make_item(tok, rng, 0)
        car = [i for i, mk in enumerate(mask) if mk]
        if len(car) < 2:
            continue                                   # binding 只在多載體有定義
        cpos = [pos[t * SPAN:(t + 1) * SPAN].tolist() for t in range(len(car))]
        Zb, Mb = Z.unsqueeze(0).to(DEVICE), M.unsqueeze(0).to(DEVICE)

        kprime, box = [], {"i": 0}

        def fn(xk, xv):
            i = box["i"]; box["i"] = (i + 1) % nl
            T = xk.shape[1]
            k, v = dl(i, Zb[:, :T], Mb[:, :T], xk, xv)
            p = pos.to(DEVICE)
            kn, kp = xk[0, p], k[0, p]
            norms["K_nat"].append(float(kn.norm()))
            norms["dK"].append(float((kp - kn).norm()))
            norms["Kp"].append(float(kp.norm()))
            norms["cos_Kp_Knat"].append(float(F.cosine_similarity(
                kp.flatten(), kn.flatten(), dim=0)))
            kprime.append(k[0].detach())
            return torch.arange(T, device=xk.device), k, v

        with Capture() as cap:
            cap.on = True
            out = m(ids.unsqueeze(0).to(DEVICE), use_cache=True,
                    num_loops=loops, kv_override=fn)
            cap.on = False
            am, am_slot = attn_mass(cap.qk, cpos)

        # carrier 之間的 K' 相似度（同一 slot 內，兩兩 cos 的平均）
        sims = []
        for kp in kprime:
            vecs = [kp[torch.as_tensor(pp, device=kp.device)].flatten() for pp in cpos]
            for a in range(len(vecs)):
                for b in range(a + 1, len(vecs)):
                    sims.append(float(F.cosine_similarity(vecs[a], vecs[b], dim=0)))

        # 模型實際講出來的是哪一條 carrier 的值？
        pkv, got = out.past_key_values, []
        for _ in range(4):
            nx = out.logits[:, -1].argmax(-1, keepdim=True)
            if nx.item() == tok.eos_token_id:
                break
            got.append(nx.item())
            out = m(nx, past_key_values=pkv, use_cache=True, num_loops=loops)
            pkv = out.past_key_values
        pred = tok.decode(got, skip_special_tokens=True).strip()
        vals = [B.val_str(ep.facts[i][2]) for i in car]
        sel = vals.index(pred) if pred in vals else -1      # -1 = 不是任何載體的值
        want = car.index(ep.ask_idx[0])

        rows.append({"n_car": len(car), "sel": sel, "want": want,
                     "am": am, "am_argmax": int(max(range(len(am)), key=lambda t: am[t])),
                     "sim": sum(sims) / len(sims) if sims else None,
                     "am_slot": am_slot,
                     "correct": int(sel == want)})

    import statistics as st
    print("  ---- 範數（逐 slot × 題 × carrier 位置）")
    for k in ("K_nat", "dK", "Kp", "cos_Kp_Knat"):
        a = sorted(norms[k])
        q = lambda f: a[int(f * (len(a) - 1))]
        print(f"    {k:>12s}  P10={q(.1):9.3f}  P50={q(.5):9.3f}  P90={q(.9):9.3f}")
    print(f"    ||ΔK||/||K_nat|| 中位數 = "
          f"{st.median(norms['dK'])/st.median(norms['K_nat']):.2f}×")
    sims = [r["sim"] for r in rows if r["sim"] is not None]
    print(f"    carrier 間 K' 相似度 cos 中位數 = {st.median(sims):.4f}"
          "   （越接近 1 = 越難分辨是哪一格）")

    print("\n  ---- attention mass（answer query → 各 carrier，對 loop/layer/head 平均）")
    for nc in sorted({r["n_car"] for r in rows}):
        sub = [r for r in rows if r["n_car"] == nc]
        mx = st.median([max(r["am"]) for r in sub])
        mn = st.median([min(r["am"]) for r in sub])
        tgt = st.median([r["am"][r["want"]] for r in sub])
        oth = st.median([sum(r["am"][t] for t in range(nc) if t != r["want"]) / (nc - 1)
                         for t in sub for r in [t]])
        print(f"    n_carrier={nc} (n={len(sub)})  max={mx:.4f}  min={mn:.4f}  "
              f"目標格={tgt:.4f}  非目標平均={oth:.4f}")

    print("\n  ---- **預測力**（這才是裁決 Codex [135] 那條解釋的關鍵）")
    ans = [r for r in rows if r["sel"] >= 0]
    print(f"    輸出是「某條載體的值」的比例 = {len(ans)}/{len(rows)} = {len(ans)/len(rows):.1%}")
    if ans:
        # ⚠️ 亂猜基準必須是**混合期望** `E[1/n_car]`，不是 `1/n` 的中位數。
        #    初版用中位數印出 50.0%，會讓「恰好等於亂猜」看起來像「低於亂猜」。
        base = sum(1 / r["n_car"] for r in ans) / len(ans)
        hit = sum(1 for r in ans if r["am_argmax"] == r["sel"])
        lo, hi = wilson(hit, len(ans))
        print(f"    attention argmax == 實際選中的載體：{hit}/{len(ans)} = {hit/len(ans):.1%}"
              f"  95% CI [{lo:.1%},{hi:.1%}]   亂猜基準 E[1/n_car] = {base:.1%}"
              + ("   → **完全不預測**" if lo <= base <= hi else "   → 偏離亂猜"))
        hit_t = sum(1 for r in ans if r["am_argmax"] == r["want"])
        print(f"    attention argmax == 目標載體    ：{hit_t}/{len(ans)} = {hit_t/len(ans):.1%}")
        # 配對比較：目標格的 attention 是否高於同題非目標格的平均
        diffs = [r["am"][r["want"]] - (sum(r["am"]) - r["am"][r["want"]]) / (r["n_car"] - 1)
                 for r in ans]
        mu = st.mean(diffs); se = st.pstdev(diffs) / len(diffs) ** 0.5
        print(f"    配對差（目標 − 同題非目標平均）= {mu:+.5f} ±{1.96*se:.5f}"
              + ("   → 顯著" if mu - 1.96 * se > 0 else "   → **不顯著**"))
        # 逐 slot 檢查：平均掉 32 個 (loop,layer) 可能蓋掉某個真的在做選擇的 slot
        if ans[0].get("am_slot"):
            S = len(ans[0]["am_slot"])

            def slot_acc(sel_list, s):
                return sum(1 for r, sl in zip(ans, sel_list)
                           if max(range(r["n_car"]),
                                  key=lambda t: r["am_slot"][s][t]) == sl) / len(ans)

            per = [slot_acc([r["sel"] for r in ans], s) for s in range(S)]
            best, bi = max(per), per.index(max(per))
            # ⚠️ **不可以**拿單次比較的 CI 去判「32 個 slot 取最大」——那是 winner's curse：
            #    32 個雜訊估計的最大值，期望值本來就遠高於基準（實測 null 的 P50 已達 47.5%）。
            #    必須跟「同樣取最大」的 permutation null 比。初版用 Wilson CI，誤報成顯著。
            rr = random.Random(20260806)
            null = sorted(max(slot_acc([rr.randrange(r["n_car"]) for r in ans], s)
                              for s in range(S)) for _ in range(2000))
            pval = sum(1 for x in null if x >= best) / len(null)
            print(f"    **逐 slot 最佳**（{S} 個 (loop,layer) 取最好，slot={bi}）：{best:.1%}"
                  f"   全 slot 平均 {sum(per)/S:.1%}")
            print(f"      permutation null 的 max：P50={null[len(null)//2]:.1%} "
                  f"P95={null[int(.95*len(null))]:.1%}   p={pval:.4f}"
                  + ("   → 顯著，有 slot 在做選擇" if pval < 0.05
                     else "   → **不顯著（winner's curse）**，沒有 slot 在做選擇"))
    print("\n  判讀：")
    print("    attention 近均勻且**不預測**選中誰 → 淹沒只是必要條件，")
    print("      下一步是**顯式 slot identity／binding channel**，不是 4-D mask")
    print("    attention 顯著預測選中誰 → 選擇發生在 attention，可沿這條修")
    print("    兩者皆非 → **撤回「native address 被淹沒」這條解釋**")

    json.dump({"tag": tag, "n": len(rows),
               "norm_median": {k: st.median(v) for k, v in norms.items()},
               "sim_median": st.median(sims), "rows": rows},
              open(os.path.join(HERE, f"results_bridge_diag_binding{tag}.json"), "w"),
              indent=2, ensure_ascii=False)
    print(f"\n  -> results_bridge_diag_binding{tag}.json")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "_f8",
         int(sys.argv[2]) if len(sys.argv) > 2 else 300)
