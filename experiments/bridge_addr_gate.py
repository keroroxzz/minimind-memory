"""B 臂的 render gate —— **先確認凍結 core 認得 `[QADDR]` 版面，再談 B。**

`BRIDGE_PREREG_addr.md` §2。Codex [137] 明令：
若凍結 core 無法消費新的 `[QADDR]` 流形，**必須重訓 core**，
**不得把新 token 的 OOD 失敗誤稱為 B FAIL**。

§4.50 的 G4b 教訓：**只因為在 prompt 前後加了東西，oracle 天花板一度掉到 0.0%。**

這一關**不訓練、不注入任何東西**，只問一件事：

    在 `L0`（值全部寫在文字裡）之下，多加一個 `[QADDR]` 空位，
    core 的答對率會不會掉？

`[QADDR]` 用與值格**同形的 `. .`**（2 token），放在 query 的 entity 之後：

    無：  | gina a 是 7 0 | dan b 是 8 4 問 dan b 是 多少 ?
    有：  | gina a 是 7 0 | dan b 是 8 4 問 dan b . . 是 多少 ?

判讀（事前鎖）：**≥95% → 格式可用，凍結 core 繼續；<95% → 必須重訓 core。**
"""
import json
import os
import random
import sys

import torch

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import bridge_renderer as B
from bridge_delivery import greedy
from bridge_train_core import DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))


def render_qaddr(ep, with_slot):
    """L0 render；`with_slot=True` 時在 query entity 之後插入 `. .` 空位。"""
    facts = " ".join(f"| {nm} {B.ATTRS[ai]} 是 {B.val_str(v)}" for nm, ai, v in ep.facts)
    q = ep.query
    if with_slot:
        # query 形如 "<name> <attr> 是 多少" 或 "... 差 ... 是 多少"
        assert q.endswith(" 是 多少"), q
        q = q[: -len(" 是 多少")] + f" {B.PLACE} 是 多少"
    return f"{facts} 問 {q} ?"


def wilson(k, n):
    z, p = 1.96, k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, c - h), min(1.0, c + h))


@torch.no_grad()
def main(n=250):
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    blob = torch.load(os.path.join(HERE, "bridge_core.pth"), map_location="cpu")
    arch = dict(blob["arch"])
    gates = json.load(open(os.path.join(HERE, "results_bridge_gates.json")))
    loops = gates["chosen_num_loops"]
    cfg = MiniMindConfig(vocab_size=blob["vocab"], **arch)
    m = MiniMindForCausalLM(cfg).to(DEVICE).eval()
    m.load_state_dict(blob["model"])

    print(f"  B 臂 render gate（prereg: BRIDGE_PREREG_addr.md §2）")
    print(f"  凍結 core，**不訓練、不注入**；只問「多一個 [QADDR] 空位會不會弄壞版面」")
    print(f"  num_loops={loops}   n={n}/格\n")

    ep0 = B.make_episode(random.Random(1), 0)
    print(f"  範例（無）：{render_qaddr(ep0, False)}")
    print(f"  範例（有）：{render_qaddr(ep0, True)}\n")

    res = {}
    print(f"  {'j':>3s} {'[QADDR]':>9s} {'正確率':>9s} {'95% CI':>18s}  判讀")
    for j in (0, 1):
        for with_slot in (False, True):
            rng = random.Random(90210 + j)
            ok = 0
            for _ in range(n):
                ep = B.make_episode(rng, j)
                ids = torch.tensor(tok(tok.bos_token + render_qaddr(ep, with_slot),
                                       add_special_tokens=False).input_ids)
                ok += int(greedy(m, tok, ids, loops) == ep.answer)
            lo, hi = wilson(ok, n)
            tagv = "有" if with_slot else "無"
            note = ""
            if with_slot:
                note = ("**格式可用** → 凍結 core 繼續" if ok / n >= 0.95
                        else "**OOD → 必須重訓 core**（不得寫成 B FAIL）")
            print(f"  {j:>3d} {tagv:>9s} {ok/n:>8.1%} [{lo:>6.1%},{hi:>6.1%}]  {note}")
            res[f"j{j}_slot{int(with_slot)}"] = [ok, n]
        print()

    a, b = res["j0_slot0"], res["j0_slot1"]
    print(f"  j=0 的落差：{a[0]/a[1]:.1%} → {b[0]/b[1]:.1%}"
          f"  ({(b[0]/b[1]-a[0]/a[1])*100:+.1f}pp)")
    json.dump(res, open(os.path.join(HERE, "results_bridge_addr_gate.json"), "w"),
              indent=2, ensure_ascii=False)
    print("  -> results_bridge_addr_gate.json")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 250)
