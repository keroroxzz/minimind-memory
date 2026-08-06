"""latent-only core 的負對照 —— **address 到底有沒有在做選擇？**

gate 若顯示 latent 在多載體下不再貼 `1/n`，**在講任何話之前必須先過這一關**。
舊 core 的教訓（§4.51）：`n_carrier=1 → 100%` 看起來很漂亮，
但要靠 content/position shuffle 才知道那 100% 是不是捷徑。

四個臂（同一批 episode 配對，只有 latent 的內容不同）：

| 臂 | z' 怎麼給 | 預期（若 address 真的在選擇）|
|---|---|---|
| `intact` | 正常 | 基準 |
| **`addr_zero`** | **addr 欄位全歸零**，payload 不動 | **應退回 `1/n`**（沒有身份可比對）|
| **`addr_shuffle`** | **addr 在 carrier 之間錯排**，payload 不動 | **應跟著 address 走** → `cf` 高、`orig` 低 |
| `payload_zero` | payload（值）歸零，addr 不動 | 健全性：應答不出來 |

`cf`（反事實）= 「若模型跟著 address 走」該給的答案：
錯排後，帶著**被問 address** 的那個 carrier，它身上的值就是 `cf`。

- `addr_zero` 退回 `1/n` **且** `addr_shuffle` 跟著 `cf` 走
  → **address 就是打破 `1/n` 的原因**，這是可宣稱的機制。
- `addr_zero` 沒退回 `1/n` → 模型用了別的東西（順序？值分布？），**機制主張撤回**。
- `addr_shuffle` 仍答 `orig` → 有洩漏，**gate 的數字要重新檢視**。

⚠️ 只在 `j=0`、`n_carrier>=2` 上做（binding 在單載體是平凡的）。
"""
import json
import os
import random
import sys
from collections import defaultdict

import torch

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import bridge_latent_schema as S
import bridge_renderer as B
from bridge_latent_gate import gen, wilson
from bridge_train_core import DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
A = S.ADDR_DIM


def build(ep, order, arm, rng):
    """回傳 (lat, cf_slot)。`cf_slot` = 帶著「被問 address」的那個 carrier 的序號。"""
    lat = torch.stack([S.fact_latent2(*ep.facts[i]) for i in order])
    n = len(order)
    cf_slot = None
    if arm == "addr_zero":
        lat[:, :A] = 0.0
    elif arm == "addr_shuffle":
        perm = list(range(n))
        for _ in range(200):
            rng.shuffle(perm)
            if all(perm[t] != t for t in range(n)):
                break
        addr = lat[:, :A].clone()
        for t in range(n):
            lat[t, :A] = addr[perm[t]]          # 第 t 格改帶第 perm[t] 格的 address
        # 被問的 fact 在 order 裡的位置
        want = order.index(ep.ask_idx[0])
        # 誰現在帶著「被問的 address」？ perm[t] == want 的那個 t
        cf_slot = perm.index(want)
    elif arm == "payload_zero":
        lat[:, A] = 0.0
    elif arm == "value_shuffle":
        # **自洽版的內容錯排**：只搬「值」，address 與 attr 留在原位。
        # `addr_shuffle` 只搬 address 會讓 latent 自相矛盾（address 說 A、attr 說 B），
        # 那個矛盾會壓低 cf，不是乾淨的對照。
        perm = list(range(n))
        for _ in range(200):
            rng.shuffle(perm)
            if all(perm[t] != t for t in range(n)):
                break
        val = lat[:, A].clone()
        for t in range(n):
            lat[t, A] = val[perm[t]]
        want = order.index(ep.ask_idx[0])
        cf_slot = perm[want]          # 被問那格現在帶的是第 perm[want] 格的值
    elif arm == "addr_random":
        # **乾淨的「沒有可比對 address」**：換成 episode 裡沒出現過的合法 descriptor。
        # 比 addr_zero 好，因為仍是單位向量、仍在流形上，只是沒有一條對得上 query。
        used = {(nm, ai) for nm, ai, _ in ep.facts}
        pool = [(nm, ai) for nm in B.NAMES for ai in range(len(B.ATTRS))
                if (nm, ai) not in used]
        rng.shuffle(pool)
        for t in range(n):
            lat[t, :A] = S.phi(*pool[t])
    return lat, cf_slot


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

    print(f"  latent-only 負對照   {core} step={blob['step']}   n={n}/格   j=0，n_carrier>=2")
    print(f"  問題：address 是不是打破 1/n 的原因？\n")

    ARMS = ("intact", "value_shuffle", "addr_random", "addr_zero",
            "addr_shuffle", "payload_zero")
    res = {}
    for nf in (2, 3, 4):
        print(f"  ==== n_carrier={nf}   （亂猜基準 1/n = {1/nf:.1%}）")
        for arm in ARMS:
            rng = random.Random(777000 + nf)
            ok_o = ok_c = tot = 0
            for _ in range(n):
                ep = B.make_episode(rng, 0, n_fact=nf)
                order = list(range(nf)); rng.shuffle(order)
                lat, cf_slot = build(ep, order, arm, rng)
                mc = proj(lat.to(DEVICE)).unsqueeze(0)
                ids = torch.tensor(tok(tok.bos_token + S.render_latent(ep)[0],
                                       add_special_tokens=False).input_ids)
                pred = gen(m, tok, ids, loops, mc)
                orig = ep.answer
                ok_o += int(pred == orig)
                if cf_slot is not None:
                    cf = B.val_str(ep.facts[order[cf_slot]][2])
                    ok_c += int(pred == cf)
                tot += 1
            lo, hi = wilson(ok_o, tot)
            s = f"    {arm:>13s}  orig={ok_o/tot:>6.1%} [{lo:.0%},{hi:.0%}]"
            if arm in ("addr_shuffle", "value_shuffle"):
                lo2, hi2 = wilson(ok_c, tot)
                s += f"   cf(跟著address)={ok_c/tot:>6.1%} [{lo2:.0%},{hi2:.0%}]"
            if arm in ("addr_zero", "addr_random"):
                s += ("   → **落在 1/n**" if lo <= 1 / nf <= hi
                      else ("   → **低於 1/n（OOD，非機制證據）**" if hi < 1 / nf
                            else "   → 高於 1/n"))
            print(s)
            res[f"{nf}|{arm}"] = {"orig": [ok_o, tot], "cf": [ok_c, tot]}
        print()

    print("  判讀：")
    print("    addr_zero 退回 1/n **且** addr_shuffle 跟著 cf 走")
    print("      → **address 就是打破 1/n 的原因**（可宣稱的機制）")
    print("    addr_zero 沒退回 1/n → 模型用了別的東西，**機制主張撤回**")
    print("    addr_shuffle 仍答 orig → 有洩漏，**gate 的數字要重新檢視**")
    json.dump(res, open(os.path.join(HERE, "results_bridge_latent_shuffle_v2.json"), "w"),
              indent=2, ensure_ascii=False)
    print("\n  -> results_bridge_latent_shuffle_v2.json")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 250)
