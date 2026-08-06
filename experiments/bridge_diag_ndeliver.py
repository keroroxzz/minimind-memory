"""`n_placeholder × n_delivered` factorial —— 解耦「prompt 有幾個空格」與「注入幾條 latent」。

目前這兩者**恆等**，所以「多載體崩塌」同時相容於兩個原因，現有資料**分不開**：

1. **多條 latent 互相競爭**（binding loss 的來源在**交付端**）
2. **prompt 裡多個空 placeholder 本身**就讓 core 混淆（來源在**表示端／prompt OOD**）

解耦只需要在 `M` 上把不注入的載體位置設 0 —— **不改模型、不重訓、純評估**。

### 設計（Codex [134] 指定，不得只跑一格）

- `n_placeholder ∈ {1,2,3,4}` × `n_delivered ∈ {0,1,2,4}`，**完整網格**。
- **`n_fact` 固定為 4**，所以**每一格的 prompt 長度與 fact 數完全相同**，
  只有「幾條寫成 `. .`」在變 —— 這樣 `n_placeholder` 的效應才不混到長度。
- 同一批 episode 跨格共用（配對），**每格重新隨機化合法 slot**。
- 答案需要的 fact **一律是載體**；`n_delivered ≥ n_used` 時交付集合 =
  `used ∪ 隨機 distractor` 補到 `n_delivered`。
- `n_delivered < n_used`（含 0）→ **本來就答不出來**，另列 `unanswerable`，
  作地板控制，不與主格混報。

### 判讀（事前寫下）

- 固定 `n_placeholder=4`、隨 **`n_delivered`** 下降 → **active-carrier competition / binding**。
- 固定 `n_delivered=1`、隨 **`n_placeholder`** 仍下降 → **空 placeholder／模板效應或 prompt OOD**。
- 兩者都掉 → 兩個原因並存，不得只認一個。

⚠️ **`n_delivered=1` 的高分只是 content transport，不是 binding**（Codex [134]）——
   只注一條同樣讓位置捷徑變平凡。**`n_delivered ≥ 2` 才有 binding 判別力。**

⚠️ post-hoc 診斷，用途是**指出下一個實驗**，不是結論。
"""
import json
import os
import random
import sys
from collections import defaultdict

import torch

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import bridge_renderer as B
from bridge_delivery import SPAN, Delivery, greedy
from bridge_train_core import DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
N_FACT = 4                      # 固定 —— 讓 prompt 長度不隨 n_placeholder 變
PLACEHOLDERS = (1, 2, 3, 4)
DELIVERED = (0, 1, 2, 4)


def build(tok, ep, n_pl, n_del, rng):
    """回傳 (ids, pos, Z, M, n_used, ok_answerable)。

    載體集合：`used` 一律入選，再從其他 fact 補到 `n_pl`（隨機化合法 slot）。
    交付集合：`used` 一律入選，再從其他**載體**補到 `n_del`。
    """
    used = list(ep.ask_idx)
    if len(used) > n_pl:
        return None
    others = [i for i in range(len(ep.facts)) if i not in used]
    rng.shuffle(others)
    car = sorted(used + others[:n_pl - len(used)])
    mask = [i in car for i in range(len(ep.facts))]

    ids, pos = B.value_positions(tok, ep, mask)
    lat = B.episode_latents(ep, mask)          # 依 fact 順序，與 pos 的段落一一對應
    Z = torch.zeros(len(ids), B.LATENT_DIM)
    M = torch.zeros(len(ids), 1)
    Z[pos] = torch.tensor(lat, dtype=torch.float32).repeat_interleave(SPAN, dim=0)

    answerable = n_del >= len(used)
    if answerable:
        pool = [i for i in car if i not in used]
        rng.shuffle(pool)
        deliver = set(used) | set(pool[:n_del - len(used)])
    else:
        deliver = set(sorted(used)[:n_del])    # 不足以答題，只作地板
    for t, fi in enumerate(car):
        if fi in deliver:
            M[pos[t * SPAN:(t + 1) * SPAN]] = 1.0
    return ids, pos, Z, M, len(used), answerable


@torch.no_grad()
def main(tag="_f8", n=250):
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
    print(f"  {tag}  nfreq={d.get('nfreq',0)}  mode={d.get('mode','kv')}  "
          f"num_loops={loops}   n={n}/格   n_fact 固定={N_FACT}")
    print("  ⚠️ post-hoc；解耦 n_placeholder×n_delivered，不改模型、不重訓")
    print("  ⚠️ n_delivered=1 的高分只是 content transport，**不是 binding**\n")

    res = {}
    for j in (0, 1):
        # 同一批 episode 跨所有格共用（配對）
        rng0 = random.Random(20260806 + j)
        eps = [B.make_episode(rng0, j, n_fact=N_FACT) for _ in range(n)]
        n_used = len(eps[0].ask_idx)
        acc = defaultdict(lambda: [0, 0])
        unans = defaultdict(lambda: [0, 0])
        for n_pl in PLACEHOLDERS:
            if n_pl < n_used:
                continue
            for n_del in DELIVERED:
                if n_del > n_pl:
                    continue
                rng = random.Random(999 + 31 * n_pl + n_del)
                for ep in eps:
                    b = build(tok, ep, n_pl, n_del, rng)
                    if b is None:
                        continue
                    ids, pos, Z, M, nu, answerable = b
                    ok = int(greedy(m, tok, ids, loops, dl, Z, M, nl) == ep.answer)
                    tgt = acc if answerable else unans
                    tgt[(n_pl, n_del)][0] += ok
                    tgt[(n_pl, n_del)][1] += 1
        print(f"  ==== j={j}   (n_used={n_used})   直欄 = n_delivered，橫列 = n_placeholder")
        print("      " + "".join(f"{'n_del='+str(k):>12s}" for k in DELIVERED))
        for n_pl in PLACEHOLDERS:
            if n_pl < n_used:
                continue
            row = ""
            for n_del in DELIVERED:
                c = acc.get((n_pl, n_del)) or unans.get((n_pl, n_del))
                if c is None or c[1] == 0:
                    row += f"{'—':>12s}"
                else:
                    star = "" if (n_pl, n_del) in acc else "*"
                    row += f"{c[0]/c[1]:>11.1%}{star}"
            print(f"  n_pl={n_pl}" + row)
        print("  （* = n_delivered < n_used，本來就答不出來的地板格）")
        res[j] = {"n_used": n_used,
                  "answerable": {f"{a}|{b}": v for (a, b), v in acc.items()},
                  "unanswerable": {f"{a}|{b}": v for (a, b), v in unans.items()}}
        print()

    print("  判讀（事前寫下）：")
    print("    固定 n_placeholder=4、隨 n_delivered 下降 → active-carrier competition / binding")
    print("    固定 n_delivered=1、隨 n_placeholder 仍下降 → 空 placeholder／模板效應或 prompt OOD")
    print("    兩者都掉 → 兩個原因並存，不得只認一個")
    json.dump(res, open(os.path.join(HERE, f"results_bridge_diag_ndeliver{tag}.json"),
                        "w"), indent=2, ensure_ascii=False)
    print(f"\n  -> results_bridge_diag_ndeliver{tag}.json")


if __name__ == "__main__":
    # 第二個參數是 n。少了它會靜默跑滿 250/格（約 5250 次前向，20 分鐘以上），
    # 看起來像當掉 —— smoke 時務必給小 n。
    main(sys.argv[1] if len(sys.argv) > 1 else "_f8",
         int(sys.argv[2]) if len(sys.argv) > 2 else 250)
