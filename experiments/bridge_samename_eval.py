"""同實體多屬性的評估 —— **按 entry provenance 分類，不只按數值**（Codex [145]）。

`BRIDGE_PREREG_retrieval.md` 的 transfer-boundary extension 一節。

### 為什麼不能只按數值

若兩條 entry 剛好同值，「數值答對」**不能冒充 binding** —— 模型可能挑錯 entry
卻因為值相同而看起來對。所以：

- 逐題記錄**每條 entry 的 (name, attr, value)**；
- 輸出的值對應到**哪一條 entry**（可能多條，那就是碰撞）；
- 碰撞題另計，report **排除碰撞的 `exact_nc`**。

### 錯誤分類（provenance）

| 類別 | 意義 |
|---|---|
| `target` | 答對被問的那條 entry |
| `same_name` | 答成**同名不同屬性**的那條 —— **屬性沒分開** |
| `other` | 答成其他 candidate —— 選擇錯誤 |
| `third` | 不是任何 entry 的值 —— **消費崩潰／混合** |
| `invalid` | 格式不合法 |

`third` 另報「是否落在兩值之間」與「距兩值平均的距離」——
舊 core 在同名時 90.5% 落在兩值之間、距平均中位數 2.5，**那是 value 混合的簽名**。

### factorial

**`dup=0`（全不同名）vs `dup>=1`（至少一組同名），分開報，不得混進整體平均。**
`n ∈ {2,4}`、`j ∈ {0,1}`。
"""
import json
import os
import random
import statistics as st
import sys

import torch

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import bridge_latent_schema as S
import bridge_renderer as B
from bridge_latent_gate import gen, wilson
from bridge_train_core import DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))


def parse(s):
    p = s.split()
    return int(p[0]) * 10 + int(p[1]) if (
        len(p) == 2 and all(len(x) == 1 and x.isdigit() for x in p)) else None


@torch.no_grad()
def main(core="bridge_core_snft.pth", n=250):
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    blob = torch.load(os.path.join(HERE, core), map_location="cpu")
    arch = dict(blob["arch"])
    cfg = MiniMindConfig(vocab_size=blob["vocab"], **arch)
    m = MiniMindForCausalLM(cfg).to(DEVICE).eval()
    m.load_state_dict(blob["model"])
    loops = arch["num_loops"]
    proj = S.CarrierProj(arch["hidden_size"]).to(DEVICE)
    proj.scale.fill_(blob["carrier_scale"])

    print(f"  同實體多屬性評估   {core}  step={blob['step']}  n={n}/格")
    print("  分類按 **entry provenance**，不只按數值；碰撞題另計\n")

    res = {}
    for j in (0, 1):
        for nf in (2, 4):
            for dup in (False, True):
                rng = random.Random(51000 + j * 13 + nf * 7 + int(dup))
                cnt = {k: 0 for k in ("target", "same_name", "other", "third", "invalid")}
                tot = nc_ok = nc_n = 0
                blend_in = blend_n = 0
                bl_d = []
                for _ in range(n):
                    ep = B.make_episode(rng, j, n_fact=nf, same_name=(True if dup else False))
                    order = list(range(nf)); rng.shuffle(order)
                    lat, _ = S.episode_latents2(ep, order)
                    mc = proj(lat.to(DEVICE)).unsqueeze(0)
                    ids = torch.tensor(tok(tok.bos_token + S.render_latent(ep)[0],
                                           add_special_tokens=False).input_ids)
                    pred = parse(gen(m, tok, ids, loops, mc))
                    # ⚠️ `same_name=True` 只保證**題目裡**有一組同名，
                    #    不保證**被問的那條**有同名兄弟 —— n=4 時目標只有約一半
                    #    機率落在那組裡，效應會被稀釋。正確的條件是「目標有同名兄弟」。
                    tgt0 = ep.ask_idx[0]
                    amb = any(f[0] == ep.facts[tgt0][0] and i != tgt0
                              for i, f in enumerate(ep.facts))
                    if dup and not amb:
                        continue                 # 只計目標本身歧義的題
                    tot += 1
                    if pred is None:
                        cnt["invalid"] += 1
                        continue
                    ans = parse(ep.answer)
                    # 哪些 entry 的值等於 pred（可能多條 → 碰撞）
                    hits = [i for i, (nm, ai, v) in enumerate(ep.facts) if v == pred]
                    tgt_i = ep.ask_idx[0]
                    tgt_nm = ep.facts[tgt_i][0]
                    # 碰撞排除：只在「pred 唯一對應一條 entry」時計入 exact_nc
                    if j == 0:
                        if len(hits) <= 1:
                            nc_n += 1
                            nc_ok += int(pred == ans)
                    if pred == ans and (j > 0 or tgt_i in hits):
                        cnt["target"] += 1
                    elif hits:
                        # 答成某條 entry：是同名的還是其他的？
                        if any(ep.facts[i][0] == tgt_nm and i != tgt_i for i in hits):
                            cnt["same_name"] += 1
                        else:
                            cnt["other"] += 1
                    else:
                        cnt["third"] += 1
                        if j == 0 and nf == 2:
                            vs = sorted(v for _, _, v in ep.facts)
                            blend_n += 1
                            blend_in += int(vs[0] < pred < vs[-1])
                            bl_d.append(abs(pred - sum(vs) / len(vs)))
                lo, hi = wilson(cnt["target"], tot)
                tag = "目標有同名" if dup else "全不同名  "
                extra = ""
                if blend_n >= 20:
                    extra = (f"   third 落兩值間 {blend_in/blend_n:.0%}"
                             f"、距平均中位 {st.median(bl_d):.1f}")
                print(f"  j={j} n={nf} {tag}  target {cnt['target']/tot:>6.1%} "
                      f"[{lo:.0%},{hi:.0%}]  same_name {cnt['same_name']/tot:>5.1%}  "
                      f"other {cnt['other']/tot:>5.1%}  third {cnt['third']/tot:>5.1%}  "
                      f"invalid {cnt['invalid']/tot:>4.1%}" + extra)
                res[f"j{j}|n{nf}|dup{int(dup)}"] = {
                    "counts": cnt, "n": tot,
                    "exact_nc": [nc_ok, nc_n],
                    "blend": [blend_in, blend_n]}
        print()

    print("  判讀：")
    print("    dup=0 與 dup>=1 的 target 差距 → 同實體多屬性的代價（**分開報，不混平均**）")
    print("    same_name 高 → 屬性沒分開（選擇錯誤）")
    print("    third 高且落在兩值之間 → **value 混合**（消費崩潰），與選擇錯誤修法不同")
    json.dump(res, open(os.path.join(HERE, f"results_samename_{core.replace('.pth','')}.json"),
                        "w"), indent=2, ensure_ascii=False)
    print(f"  -> results_samename_{core.replace('.pth','')}.json")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "bridge_core_snft.pth",
         int(sys.argv[2]) if len(sys.argv) > 2 else 250)
