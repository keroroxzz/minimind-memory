"""A 臂的核心負對照 —— **tag 到底有沒有被拿去做綁定？**

`BRIDGE_PREREG_tag.md` §2 列為**強制、不得省略**。A 臂 primary 已判 **A-FAIL**
（逐格貼 `1/n`），但 FAIL 有兩種完全不同的原因，必須分開：

1. **tag 根本沒被用來綁定** —— 交付把它吃掉了／模型忽略它
2. **tag 被用了但不足以綁定** —— 有影響，只是壓不過內容通道

三個評估臂（同一批 episode 配對，只有 tag 的擺法不同）：

| 臂 | tag 怎麼給 | 預期（若 tag 真被用於綁定）|
|---|---|---|
| `intact` | 每條 carrier 拿自己 fact index 的碼 | 基準 |
| **`permuted`** | tag 在載體之間**錯排**，內容不動 | 應**跟著 tag 走** → `cf` 高、`orig` 低 |
| `zeroed` | tag 全部歸零 | 若與 `intact` 無異 → **tag 完全沒被用**|

`cf`（反事實）= 「若模型跟著 tag 走」該給的答案：
tag 錯排後，第 t 格拿到的是 fact `perm[t]` 的 tag，所以「跟著 tag」= 回答 fact `perm[t]` 的值。

⚠️ post-hoc 之外的部分照 prereg；`orig`/`cf` 的定義與 `BRIDGE_PREREG_shuffle.md` 一致。
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
from bridge_delivery import SPAN
from bridge_delivery_tag import TagDelivery, greedy_tag, tag_codes
from bridge_train_core import DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))


def build(tok, ep, mask, codes, mode, rng):
    """回傳 (ids, pos, Z, M, tag_owner)。`tag_owner[t]` = 第 t 格拿到誰的 tag。"""
    ids, pos = B.value_positions(tok, ep, mask)
    car = [i for i, m in enumerate(mask) if m]
    lat = torch.tensor(B.episode_latents(ep, mask), dtype=torch.float32)

    owner = list(range(len(car)))
    if mode == "permuted" and len(car) >= 2:
        for _ in range(200):
            rng.shuffle(owner)
            if all(owner[t] != t for t in range(len(car))):
                break
    if mode == "zeroed":
        tg = torch.zeros(len(car), codes.shape[1])
    else:
        tg = torch.stack([codes[car[owner[t]]] for t in range(len(car))])

    lat = torch.cat([lat, tg], dim=1)
    Z = torch.zeros(len(ids), B.LATENT_DIM + codes.shape[1])
    M = torch.zeros(len(ids), 1)
    Z[pos] = lat.repeat_interleave(SPAN, dim=0)
    M[pos] = 1.0
    return ids, pos, Z, M, owner


def wilson(k, n):
    if n == 0:
        return (0.0, 1.0)
    z, p = 1.96, k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, c - h), min(1.0, c + h))


@torch.no_grad()
def main(tag="_tagA", n=250):
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    blob = torch.load(os.path.join(HERE, "bridge_core.pth"), map_location="cpu")
    arch = dict(blob["arch"])
    cfg = MiniMindConfig(vocab_size=blob["vocab"], **arch)
    m = MiniMindForCausalLM(cfg).to(DEVICE).eval()
    m.load_state_dict(blob["model"])

    d = torch.load(os.path.join(HERE, f"bridge_delivery{tag}.pth"), map_location="cpu")
    loops, nl = d["loops"], arch["num_hidden_layers"] * arch["num_loops"]
    dl = TagDelivery(B.LATENT_DIM, nl, arch["num_key_value_heads"],
                     arch["hidden_size"] // arch["num_attention_heads"],
                     nfreq=d.get("nfreq", 0), mode=d.get("mode", "kv"),
                     tag_dim=d.get("tag_dim", 8)).to(DEVICE)
    dl.load_state_dict(d["delivery"])
    dl.eval()
    codes = d.get("codes", tag_codes())
    print(f"  {tag}   tag_dim={d.get('tag_dim')}   n={n}/臂   j=0，只取 n_carrier>=2")
    print("  prereg: BRIDGE_PREREG_tag.md §2（位置 shuffle 是 A 臂的核心對照）\n")

    ARMS = ("intact", "permuted", "zeroed")
    res = {}
    print(f"  {'臂':>10s} {'orig':>18s} {'cf(跟著tag)':>18s}")
    for arm in ARMS:
        rng = random.Random(606060)
        ok_o = ok_c = tot = 0
        by = defaultdict(lambda: [0, 0])
        tried = 0
        while tot < n and tried < n * 6:
            tried += 1
            ep = B.make_episode(rng, 0)
            mask = B.random_mask(rng, ep, p=0.6, force_used=True)
            car = [i for i, mk in enumerate(mask) if mk]
            if len(car) < 2:
                continue
            ids, pos, Z, M, owner = build(tok, ep, mask, codes, arm, rng)
            pred = greedy_tag(m, tok, ids, loops, dl, Z, M, nl)
            want = car.index(ep.ask_idx[0])
            orig = B.val_str(ep.facts[car[want]][2])
            cf = B.val_str(ep.facts[car[owner[want]]][2])   # 跟著 tag 走該給的答案
            ok_o += int(pred == orig)
            ok_c += int(pred == cf)
            by[len(car)][0] += int(pred == orig)
            by[len(car)][1] += 1
            tot += 1
        lo, hi = wilson(ok_o, tot)
        lo2, hi2 = wilson(ok_c, tot)
        print(f"  {arm:>10s} {ok_o/tot:>8.1%} [{lo:>5.1%},{hi:>5.1%}]"
              f" {ok_c/tot:>8.1%} [{lo2:>5.1%},{hi2:>5.1%}]   (n={tot})")
        res[arm] = {"orig": [ok_o, tot], "cf": [ok_c, tot],
                    "by_ncar": {str(k): by[k] for k in by}}

    print("\n  判讀：")
    io, po = res["intact"]["orig"], res["permuted"]["orig"]
    zo = res["zeroed"]["orig"]
    pc = res["permuted"]["cf"]
    li, hi_ = wilson(*io); lz, hz = wilson(*zo)
    print(f"    zeroed vs intact：{zo[0]/zo[1]:.1%} vs {io[0]/io[1]:.1%}"
          + ("   → **tag 對結果沒有影響**（CI 重疊）"
             if not (lz > hi_ or hz < li) else "   → tag 有影響"))
    lp, hp = wilson(*po); lpc, hpc = wilson(*pc)
    print(f"    permuted：orig {po[0]/po[1]:.1%}  vs  cf(跟著tag) {pc[0]/pc[1]:.1%}"
          + ("   → **沒有跟著 tag 走**（tag 未用於綁定）"
             if not (lpc > hp) else "   → 跟著 tag 走，tag 確實在綁定"))
    json.dump(res, open(os.path.join(HERE, f"results_bridge_tag_shuffle{tag}.json"),
                        "w"), indent=2, ensure_ascii=False)
    print(f"\n  -> results_bridge_tag_shuffle{tag}.json")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "_tagA",
         int(sys.argv[2]) if len(sys.argv) > 2 else 250)
