"""**干預診斷，不是新 milestone、不救 MN1。** routing 走 `addr` 還是走 `attr` one-hot？

`bridge_addr_confusion.py` 給的是描述：easy 的錯誤 100% 條件於 attr 碰撞。
描述不能定因果，所以這裡做**欄位消融**——凍結 core，零訓練，只改注入 latent 的欄位。

`z' = [addr(16) , value(1) , attr one-hot(3)]`。注意 **addr 本身已經編碼 attr**
（`φ(name, attr)`），所以把 one-hot 歸零並不刪掉 attr 資訊，只是逼模型改從 addr 讀。

| 消融 | 若 routing 靠 attr one-hot | 若 routing 靠 addr |
|---|---|---|
| `zero_attr` | 崩 | 不崩 |
| `zero_addr` | 不崩 | 崩 |

兩個消融方向相反，**互相驗證**；只有一個崩才算乾淨。兩個都崩 = 只是 OOD 敏感，
不能歸因（消融本身讓輸入離開訓練分布，這是本設計的已知限制，必須跟著結論一起報）。

stratum：`hard`（同名／異 attr，MN1 = 100%）與 `easy-diff`（異名／異 attr，99.6%）
與 `easy-same`（異名／同 attr，36%）。第三格作為地板參照。
"""
import json, os, random, sys
import torch
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer
import bridge_latent_schema as S
import bridge_renderer as B
from bridge_latent_gate import gen, wilson
from bridge_heldout_eval import build, parse
from bridge_train_core import DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
A = S.ADDR_DIM


def ablate(lat, how):
    """`zero_*` 會離開流形（見檔頭限制）；`swap_*` 不會——只在兩條 carrier 之間搬欄位，
    latent 集合的分佈**逐元素完全不變**，所以答案跑去哪裡是乾淨的歸因。"""
    lat = lat.clone()
    if how == "zero_attr":
        lat[:, A + 1:] = 0.0
    elif how == "zero_addr":
        lat[:, :A] = 0.0
    elif how == "swap_attr":
        lat[:, A + 1:] = lat[:, A + 1:].flip(0)
    elif how == "swap_addr":
        lat[:, :A] = lat[:, :A].flip(0)
    return lat


def pick(rng, src, allp, stratum):
    """回傳 (target, distractor)。三個 stratum 各自的取樣規則。

    ⚠️ **candidate 一律取自 `src`**。舊版 `hard` 取自 `allp`，會讓 distractor 落到
    held-out 組合，那一列就不是嚴格的 seen×seen（Codex [156] 回覆的實作審計）。
    """
    for _ in range(1000):
        tgt = src[rng.randrange(len(src))]
        if stratum == "hard":                       # 同名、異 attr
            c = [p for p in src if p[0] == tgt[0] and p != tgt]
        elif stratum == "easy-diff":                # 異名、異 attr
            c = [p for p in src if p[0] != tgt[0] and p[1] != tgt[1]]
        else:                                       # easy-same：異名、同 attr
            c = [p for p in src if p[0] != tgt[0] and p[1] == tgt[1]]
        if c:
            dis = c[rng.randrange(len(c))]
            assert tgt in src and dis in src, "target/distractor 必須都在 seen 組合內"
            return tgt, dis
    raise RuntimeError(f"stratum={stratum} 在 src 內找不到合法配對")


@torch.no_grad()
def main(core="bridge_core_ho.pth", n=200):
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    blob = torch.load(os.path.join(HERE, core), map_location="cpu")
    arch = dict(blob["arch"])
    m = MiniMindForCausalLM(MiniMindConfig(vocab_size=blob["vocab"], **arch)).to(DEVICE).eval()
    m.load_state_dict(blob["model"])
    loops = arch["num_loops"]
    proj = S.CarrierProj(arch["hidden_size"]).to(DEVICE)
    proj.scale.fill_(blob["carrier_scale"])

    allp = [(nm, ai) for nm in B.NAMES for ai in range(len(B.ATTRS))]
    src = [p for p in allp if p not in B.heldout_split()]     # seen only，單一變因
    print(f"  欄位消融（干預診斷）  {core} step={blob['step']}  seen 組合  n={n}/格")
    print("  `addr` 已編碼 attr，故 zero_attr **不刪 attr 資訊**，只逼它改從 addr 讀\n")
    MODES = ("intact", "zero_attr", "zero_addr", "swap_attr", "swap_addr")
    print(f"  每格報 目標值%／distractor值%（其餘為第三值）\n")
    print(f"  {'stratum':>11s} " + " ".join(f"{h:>15s}" for h in MODES))

    res = {}
    for stratum in ("hard", "easy-diff", "easy-same"):
        row, cells = [], {}
        for how in MODES:
            rng = random.Random(93000)              # **同一 seed** → 各欄是同一批 episode
            ok = bad = 0
            for _ in range(n):
                tgt, dis = pick(rng, src, allp, stratum)
                ep, tv, dv = build(rng, tgt, dis)
                order = [0, 1]; rng.shuffle(order)
                lat, _ = S.episode_latents2(ep, order)
                ids = torch.tensor(tok(tok.bos_token + S.render_latent(ep)[0],
                                       add_special_tokens=False).input_ids)
                mc = proj(ablate(lat, how).to(DEVICE)).unsqueeze(0)
                p = parse(gen(m, tok, ids, loops, mc))
                ok += int(p == tv)
                bad += int(p == dv)
            row.append(f"{ok/n:>6.1%}/{bad/n:<8.1%}")
            cells[how] = {"target": ok, "dis": bad, "n": n}
        print(f"  {stratum:>11s} " + " ".join(row))
        res[stratum] = cells

    print("\n  判讀（以 `swap_*` 為準——`zero_*` 會離開流形，只作參考）：")
    print("    `swap_attr` 讓答案跑到 **distractor 值** → routing 由 attr one-hot 決定。")
    print("    `swap_addr` 讓答案跑到 distractor 值 → routing 由 addr 決定。")
    print("    兩者都不搬 → 值不是靠欄位比對取出的，兩個描述都推翻。")
    # 檔名帶 core stem —— 這個診斷的重點就是**跨 core 對照**，共用檔名會互相覆蓋
    out = f"results_bridge_field_ablation_{os.path.splitext(core)[0]}.json"
    json.dump({"core": core, "n": n, "strata": res},
              open(os.path.join(HERE, out), "w"), indent=2, ensure_ascii=False)
    print(f"  -> {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "bridge_core_ho.pth",
         int(sys.argv[2]) if len(sys.argv) > 2 else 200)
