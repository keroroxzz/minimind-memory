"""in-place KV 版的 oracle sanity（Codex 設計的 ladder 第 1 階，**零學習**）。

從 L0 forward 抓下 **16 個 loop×layer 位置** 上、value 那 5 個 token 位置的
**原生 K/V**，再注入 latent skeleton 的**同樣位置**。位置/mask/token 骨架不變。

判讀：
  **達不到近 100% → 先修 replacement / RoPE / mask / 索引，禁止訓 synthesizer。**
  近 100% → 才進第 2 階（move-only：重用已訓好的 InlineLatentAdapter）
"""
import os
import sys

import torch

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import g1_renderer as R
from g1_oracle_inline import value_positions
from g1_train import ARCH, BACKBONE, DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))


@torch.no_grad()
def capture_teacher_kv(model, ids, pos, num_loops):
    """在 L0 的 token 上跑一次，抓下每個 (loop,layer) 位置在 `pos` 上的 K/V。"""
    out = model(ids.unsqueeze(0).to(DEVICE), use_cache=True, num_loops=num_loops)
    kv = out.past_key_values                      # 16 × (K, V)，各 (1, T, n_kv, hd)
    p = pos.to(DEVICE)
    return [(p, k[:, p].clone(), v[:, p].clone()) for k, v in kv]


@torch.no_grad()
def greedy_override(model, tok, ids, override, num_loops, max_new=8):
    out = model(ids.unsqueeze(0).to(DEVICE), use_cache=True, num_loops=num_loops,
                kv_override=override)
    pkv, got = out.past_key_values, []
    for _ in range(max_new):
        nxt = out.logits[:, -1].argmax(-1, keepdim=True)
        if nxt.item() == tok.eos_token_id:
            break
        got.append(nxt.item())
        out = model(nxt, past_key_values=pkv, use_cache=True, num_loops=num_loops)
        pkv = out.past_key_values
    return tok.decode(got, skip_special_tokens=True).strip()


def main():
    ck = sys.argv[1] if len(sys.argv) > 1 else "g1_L0_b5d9b10c5b.pth"
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    cfg = MiniMindConfig(**BACKBONE, **ARCH)
    assert cfg.use_looped_transformer and cfg.num_loops == 2
    m = MiniMindForCausalLM(cfg).to(DEVICE).eval()
    _, unexp = m.load_state_dict(torch.load(os.path.join(HERE, ck), map_location="cpu"),
                                 strict=False)
    assert not unexp
    print(f"  core = {ck}（凍結，**零可學參數**）")
    nL = BACKBONE["num_hidden_layers"] * ARCH["num_loops"]

    tr = R.build_dataset(4, 5000, 42)
    va = R.build_dataset(4, 100, 42 + 999, exclude={s.delivery_id for s in tr})
    print(f"  val {len(va)}（與 L0 正式同一批：{R.delivery_checksum(va)}）\n")

    from collections import defaultdict
    res = {c: defaultdict(lambda: [0, 0]) for c in ("L0", "latent_raw", "teacher_kv_all16",
                                                    "teacher_kv_loop0")}
    for s in va:
        a_ids, b_ids, pos = value_positions(tok, s)
        ans = R.render_L0(s)[1]
        teach = capture_teacher_kv(m, a_ids, pos, ARCH["num_loops"])
        loop0 = [teach[i] if i < BACKBONE["num_hidden_layers"] else None for i in range(nL)]
        for name, ids_, ovr in (("L0", a_ids, None), ("latent_raw", b_ids, None),
                                ("teacher_kv_all16", b_ids, teach),
                                ("teacher_kv_loop0", b_ids, loop0)):
            g = greedy_override(m, tok, ids_, ovr, ARCH["num_loops"])
            r = res[name][s.k]
            r[1] += 1; r[0] += (g == ans)

    print(f"  {'條件':<20s} " + "  ".join(f"k={k}" for k in sorted(res['L0'])) + "     整體")
    for name in ("L0", "latent_raw", "teacher_kv_all16", "teacher_kv_loop0"):
        d = res[name]
        cells = "  ".join(f"{d[k][0]/d[k][1]:5.1%}" for k in sorted(d))
        ov = sum(v[0] for v in d.values()) / sum(v[1] for v in d.values())
        print(f"  {name:<20s} {cells}    {ov:6.1%}")
    print("\n  teacher_kv_all16 = 16 個 loop×layer 位置全換（primary）"
          "\n  teacher_kv_loop0 = 只換 loop0 的 8 層（ablation，非主條件）")


if __name__ == "__main__":
    main()
