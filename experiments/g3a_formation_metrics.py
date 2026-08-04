"""G3a formation 指標（獨立腳本，可套用到任何一版 writer 的 checkpoint）。

只看 end-to-end 不足以判讀 write formation（Codex）：writer 可能在
「幾乎對」與「完全沒學到」之間，而 end-to-end 都是接近地板。
所以在**未見置換**上另報四個量：

  row argmax accuracy   逐列選對哪一欄 —— formation 本身的準確度
  合法 permutation 率   5 列的 argmax 互不碰撞 —— 逐列 softmax 不保證欄唯一
  row entropy           0 = 完全確定，ln5 = 1.609 為均勻
  latent 距離           與 `perm_to_latent` 的 max|diff| 中位數

chance：row argmax 20%、合法 permutation 率 5!/5^5 = 3.84%。
"""
import argparse
import json
import math
import os
import sys

import torch

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import g1_renderer as R
from g1_train import ARCH, BACKBONE, DEVICE, make_delivery
from g3a_train import CORE_ZD, WRITE_KEYS, Writer, precompute_events
from model.memory_module import PERM_N, perm_to_latent
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))


@torch.no_grad()
def formation(writer, ev, perms, out_param):
    gold, pred, ent, dist = [], [], [], []
    for kk in WRITE_KEYS:
        for p in perms:
            z = writer(ev[(kk, tuple(p))].unsqueeze(0))[0]
            mat = z.view(PERM_N, PERM_N)
            pr = mat if out_param == "rowsoftmax" else mat.softmax(-1)
            gold.append(torch.tensor(p)); pred.append(mat.argmax(-1).cpu())
            ent.append(float(-(pr.clamp_min(1e-9).log() * pr).sum(-1).mean()))
            dist.append(float((z.cpu() - perm_to_latent(list(p))).abs().max()))
    gold, pred = torch.stack(gold), torch.stack(pred)
    return {
        "n": len(pred),
        "row_argmax_acc": (gold == pred).float().mean().item(),
        "exact_perm_acc": (gold == pred).all(-1).float().mean().item(),
        "valid_perm_rate": sum(len(set(r.tolist())) == PERM_N for r in pred) / len(pred),
        "row_entropy": sum(ent) / len(ent),
        "latent_maxdiff_median": sorted(dist)[len(dist) // 2],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ckpts", nargs="+", help="g3a_writer_*.pth（可多個，逐一比較）")
    ap.add_argument("--out-param", nargs="+", default=None,
                    help="每個 checkpoint 的輸出參數化，預設全部 free")
    a = ap.parse_args()
    ops = a.out_param or ["free"] * len(a.ckpts)
    assert len(ops) == len(a.ckpts)

    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    m = MiniMindForCausalLM(MiniMindConfig(**BACKBONE, **ARCH)).to(DEVICE).eval()
    m.load_state_dict(torch.load(os.path.join(HERE, CORE_ZD), map_location="cpu")["model"],
                      strict=False)
    p_tr, p_va = R.perm_splits()
    ev_tr = precompute_events(m, tok, p_tr)
    ev_va = precompute_events(m, tok, p_va)

    print(f"  chance：row argmax {1/PERM_N:.1%}   合法 permutation "
          f"{math.factorial(PERM_N)/PERM_N**PERM_N:.2%}   均勻 entropy {math.log(PERM_N):.3f}\n")
    print(f"  {'checkpoint':<26s} {'集合':<6s} {'row acc':>8s} {'整置換':>7s} "
          f"{'合法率':>7s} {'entropy':>8s} {'latent 距離':>11s}")
    res = {}
    for ck, op in zip(a.ckpts, ops):
        w = Writer(BACKBONE["hidden_size"], out_param=op).to(DEVICE).eval()
        w.load_state_dict(torch.load(os.path.join(HERE, ck), map_location="cpu"))
        for nm, ev, ps in (("train", ev_tr, p_tr), ("**val**", ev_va, p_va)):
            r = formation(w, ev, ps, op)
            res[f"{ck}:{nm}"] = r
            print(f"  {os.path.basename(ck)[:26]:<26s} {nm:<6s} "
                  f"{r['row_argmax_acc']:>8.1%} {r['exact_perm_acc']:>7.1%} "
                  f"{r['valid_perm_rate']:>7.1%} {r['row_entropy']:>8.3f} "
                  f"{r['latent_maxdiff_median']:>11.3f}")
    print(f"\n  val = **未見置換**。row acc ≈ 20% 表示 formation 完全沒發生；"
          f"\n  row acc 高但 end-to-end 低，表示 formation 成立而交付/消費端出問題。")
    json.dump(res, open(os.path.join(HERE, "results_g3a_formation.json"), "w"),
              indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
