"""G6c：**paired logit decomposition** —— 用可證偽的方式測 margin 假說。

§4.46 提的假說：交付造成的偏移大小固定，而任務 margin 隨合成步數縮小。
⚠️ **不可**寫成「margin < hidden 偏移」—— 不同空間、不同單位（Codex）。
正確的量法是**在 logit 空間逐位置配對**：

    m_text = logit(correct) − max_wrong        （文字 carry 的 margin）
    Δm     = m_delivery − m_text               （交付造成的 margin 位移）
    翻錯的**精確**條件： m_text + Δm < 0

**teacher-forced**：餵入 prompt + 正確答案，讀答案位置的 logit ——
避免前一位的錯誤污染後續 margin。

### 假說成立的三個條件（缺一即不成立）

1. `m_text` 隨 `j` **系統下降**
2. `Δm` 的條件分布**大致不隨 `j`**
3. `m_text + Δm < 0` **幾乎逐題預測翻錯**（覆蓋率高）

若 `Δm` 也隨 `j`/config 改變 → 機制仍是 **task-relevant directional interaction**。
若 margin 不降 → **假說直接被推翻**。
`mixed` 較差也要先問是不是**更負的 `Δm`**，不可由 hidden norm 猜。
"""
import json, os, random, sys
import torch
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer
import g1_renderer as R, g3a_train as G3, g3b_closure as G3B, g3d_scale_audit as G3D
import g5b_segmented_reasoning as G5B, g6a_zdelta_v2 as G6A
from g1_train import ARCH, BACKBONE, DEVICE, make_delivery
from model.memory_module import ADDR_DIM, PERM_N, perm_to_latent

HERE = os.path.dirname(os.path.abspath(__file__)); IDENT = list(range(PERM_N))
NL = BACKBONE["num_hidden_layers"] * ARCH["num_loops"]


@torch.no_grad()
def tf_margins(m, tok, slots, deliv, gold, dl=None):
    """teacher-forced：回傳答案每個位置的 `logit(correct) − max_wrong`。"""
    prompt = G5B.render_slots(IDENT, slots)
    pids = tok(tok.bos_token + prompt, add_special_tokens=False).input_ids
    full = tok(tok.bos_token + prompt + R._nums(gold), add_special_tokens=False).input_ids
    ids = torch.tensor(full).unsqueeze(0).to(DEVICE)
    kw = {}
    if dl is not None:
        _, pos = G5B.slot_positions(tok, IDENT, slots)
        lat = torch.stack([perm_to_latent(v) for v in deliv]).to(DEVICE).unsqueeze(0)
        kw["kv_override"] = G3._mk_fn(dl, lat, pos.to(DEVICE))
    lg = m(ids, **kw).logits[0]
    out = []
    for t in range(len(pids), len(full)):
        row = lg[t - 1].clone()
        c = full[t]; correct = row[c].item()
        row[c] = -1e9
        out.append(correct - row.max().item())
    return out


def main():
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    m, dl1, _, _, _, _ = G3D.load_all(tok, "g3a_writer_6820855741.pth",
                                      "results_g2b.json")
    dl2 = make_delivery("zdelta", BACKBONE["num_hidden_layers"], ARCH["num_loops"],
                        os.path.join(HERE, "g1_renderer_artifact.json"))
    dl2.load_state_dict(torch.load(os.path.join(HERE, "zdelta_v2.pth"),
                                   map_location="cpu")); dl2.eval()
    _, perms = R.perm_splits()
    N = 80
    print(f"  teacher-forced；`m_text` = logit(correct)−max_wrong，"
          f"`Δm` = m_delivery − m_text\n")
    print(f"  {'格子':<12s} {'ver':<4s} {'m_text 中位':>11s} {'m_text 最低':>11s} "
          f"{'Δm 中位':>9s} {'Δm 最低':>9s} {'m+Δm<0 題數':>12s} {'實際翻錯':>9s} {'覆蓋':>7s}")
    out = {}
    for j in (0, 1, 2, 3):
        for cfg in ("allph", "mixed"):
            for vn, D in (("v1", dl1), ("v2", dl2)):
                rng = random.Random(4242 + j)
                mt_all, dm_all, pred_flip, real_flip, both = [], [], 0, 0, 0
                for _ in range(N):
                    slots, deliv, gold = G6A.make_item(rng, perms, j, cfg)
                    expl = [v for v in deliv] + [s for s in slots if s is not None]
                    mt = tf_margins(m, tok, expl, [], gold)
                    md = tf_margins(m, tok, slots, deliv, gold, D)
                    n = min(len(mt), len(md))
                    dm = [md[i] - mt[i] for i in range(n)]
                    mt_all += mt[:n]; dm_all += dm
                    p = any(mt[i] + dm[i] < 0 for i in range(n))
                    r = (G5B.call_core(m, tok, IDENT, slots, D,
                                       torch.stack([perm_to_latent(v)
                                                    for v in deliv]).to(DEVICE)) != gold)
                    pred_flip += p; real_flip += r; both += (p and r)
                med = lambda x: sorted(x)[len(x)//2]
                cov = both / max(real_flip, 1)
                tag = f"j={j} {cfg}"
                print(f"  {tag:<12s} {vn:<4s} {med(mt_all):>11.3f} {min(mt_all):>11.3f} "
                      f"{med(dm_all):>9.3f} {min(dm_all):>9.3f} {pred_flip:>12d} "
                      f"{real_flip:>9d} {cov:>7.1%}")
                out[f"j{j}_{cfg}_{vn}"] = {
                    "m_text_med": med(mt_all), "m_text_min": min(mt_all),
                    "dm_med": med(dm_all), "dm_min": min(dm_all),
                    "pred_flip": pred_flip, "real_flip": real_flip, "coverage": cov}
    print(f"\n  **假說成立需三條同時**：`m_text` 隨 j 系統下降；"
          f"`Δm` 條件分布大致不隨 j；`m+Δm<0` 覆蓋率高。"
          f"\n  若 `Δm` 也隨 j/config 改變 → **task-relevant directional interaction**；"
          f"\n  若 margin 不降 → **假說直接推翻**。")
    json.dump(out, open(os.path.join(HERE, "results_g6c.json"), "w"), indent=2)
    print(f"  -> results_g6c.json")

main()
