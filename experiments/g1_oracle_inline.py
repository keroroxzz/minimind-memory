"""OracleInlineEmbedding sanity check（Codex 設計，零參數）。

把 latent render 裡的 5 個 placeholder 位置，換成**對應 value token 的原生 embedding**，
core 凍結、**沒有任何可學參數**。預期應近乎重現 L0。

判讀：
  oracle 過 + learned 過 + prefix/kv 不過  → 支持「交付位置」假說
  **oracle 不過**                          → 是 embedding replacement / position / mask
                                             的實作問題，不是位置假說
"""
import os
import sys

import torch

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import g1_renderer as R
from g1_train import ARCH, BACKBONE, DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))


def value_positions(tok, s):
    """L0 與 latent 的 token 序列只在 value span 相異（renderer 關卡 5 已逐題驗過）。"""
    a = tok(tok.bos_token + R.render_L0(s)[0], add_special_tokens=False).input_ids
    b = tok(tok.bos_token + R.render_latent(s)[0], add_special_tokens=False).input_ids
    assert len(a) == len(b), "L0 與 latent 長度必須相同"
    pos = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    assert len(pos) == s.k * R.PERM_N, f"相異位置 {len(pos)} != k×5"
    return torch.tensor(a), torch.tensor(b), torch.tensor(pos)


@torch.no_grad()
def greedy_embeds(model, tok, ids, embeds, num_loops, max_new=8):
    out = model(ids.unsqueeze(0).to(DEVICE), inputs_embeds=embeds.unsqueeze(0).to(DEVICE),
                use_cache=True, num_loops=num_loops)
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
    miss, unexp = m.load_state_dict(torch.load(os.path.join(HERE, ck), map_location="cpu"),
                                    strict=False)
    assert not unexp, f"checkpoint 不符：{unexp[:3]}"
    print(f"  core = {ck}（凍結，**零可學參數**）")

    tr = R.build_dataset(4, 5000, 42)
    va = R.build_dataset(4, 100, 42 + 999, exclude={s.delivery_id for s in tr})
    print(f"  val {len(va)}（與 L0 正式同一批：delivery checksum "
          f"{R.delivery_checksum(va)}）\n")

    emb = m.model.embed_tokens
    from collections import defaultdict
    res = {c: defaultdict(lambda: [0, 0]) for c in ("L0", "latent_raw", "oracle_inline")}
    for s in va:
        a_ids, b_ids, pos = value_positions(tok, s)
        ans = R.render_L0(s)[1]
        with torch.no_grad():
            e_a = emb(a_ids.to(DEVICE))
            e_b = emb(b_ids.to(DEVICE))
            e_o = e_b.clone()
            e_o[pos.to(DEVICE)] = e_a[pos.to(DEVICE)]      # 同位置替換，零參數
        for name, ids_, e in (("L0", a_ids, e_a), ("latent_raw", b_ids, e_b),
                              ("oracle_inline", b_ids, e_o)):
            g = greedy_embeds(m, tok, ids_, e, ARCH["num_loops"])
            r = res[name][s.k]
            r[1] += 1; r[0] += (g == ans)

    print(f"  {'條件':<16s} " + "  ".join(f"k={k}" for k in sorted(res['L0'])) + "     整體")
    for name in ("L0", "latent_raw", "oracle_inline"):
        d = res[name]
        cells = "  ".join(f"{d[k][0]/d[k][1]:5.1%}" for k in sorted(d))
        ov = sum(v[0] for v in d.values()) / sum(v[1] for v in d.values())
        print(f"  {name:<16s} {cells}    {ov:6.1%}")
    print("\n  L0 = 值在 prompt 裡；latent_raw = placeholder 且無交付（下限）；"
          "\n  oracle_inline = placeholder 位置換成原生 value embedding（零參數上限）")


if __name__ == "__main__":
    main()
