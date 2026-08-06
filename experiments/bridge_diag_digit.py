"""逐位數診斷 —— learned delivery 是不是根本分不開十位與個位？

CLAUDE.md 記過的規則：**Split accuracy by digit**。整體正確率把整個結果藏起來過
一次（ones digit 是淺鏈、tens digit 是進位傳遞，行為完全不同），這次又跳過了。

假說 H1（機制缺陷）：`ΔK/ΔV` 只是 `(slot, z)` 的函數，**與位置無關**，而一個值的
2 個 token 拿到**同一個** `z` —— 所以交付在物理上無法告訴 core「這一格是十位、
下一格是個位」。凍結的 core 當初是用 oracle inline 訓練的，那時兩格各自帶著自己的
數字 embedding。

可證偽的預測，若 H1 成立：
  (a) 十位與個位的正確率都遠低於 100%，且**兩者接近**（不是一個好一個壞）
  (b) 模型會**大量輸出兩位相同的數字**（33、77），因為兩格收到相同訊號
  (c) 預測值與真值的差是**大而發散**的，不是小而有界的

若 (b) 明顯高於資料本身的重複率，H1 基本確立，這是 bug 不是發現。
若 (a) 顯示十位近乎正確而個位隨機，那是**解析度**問題（連續 latent 的低位被
壓掉），意義完全不同 —— 那才是 G7d 那條線的證據。
"""
import json
import os
import random
import sys
from collections import Counter

import torch

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer

import bridge_renderer as B
from bridge_delivery import Delivery, greedy, make_item
from bridge_train_core import DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))


@torch.no_grad()
def main(tag=""):
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    blob = torch.load(os.path.join(HERE, "bridge_core.pth"), map_location="cpu")
    arch = dict(blob["arch"])
    cfg = MiniMindConfig(vocab_size=blob["vocab"], **arch)
    m = MiniMindForCausalLM(cfg).to(DEVICE).eval()
    m.load_state_dict(blob["model"])

    d = torch.load(os.path.join(HERE, f"bridge_delivery{tag}.pth"), map_location="cpu")
    loops, nl = d["loops"], arch["num_hidden_layers"] * arch["num_loops"]
    # ⚠️ `mode` 必須從 checkpoint 帶進來 —— 見 bridge_diag_ncarrier.py 的同一則註解。
    #    少了它，v_only/k_only 會被當成 kv 評估（形狀相同、靜默載入、結果全錯）。
    dl = Delivery(B.LATENT_DIM, nl, arch["num_key_value_heads"],
                  arch["hidden_size"] // arch["num_attention_heads"],
                  nfreq=d.get("nfreq", 0), mode=d.get("mode", "kv")).to(DEVICE)
    dl.load_state_dict(d["delivery"]); dl.eval()

    out = {}
    for j in (0, 1):
        rng = random.Random(31337 + j)
        tens = ones = both = n = 0
        errs, same_pred, same_true = [], 0, 0
        for _ in range(300):
            ep, mask, ids, pos, Z, M = make_item(tok, rng, j)
            got = greedy(m, tok, ids, loops, dl, Z, M, nl)
            g, t = got.split(), ep.answer.split()
            if len(g) != 2 or not all(x.isdigit() for x in g):
                n += 1; continue
            n += 1
            tens += int(g[0] == t[0]); ones += int(g[1] == t[1])
            both += int(g == t)
            same_pred += int(g[0] == g[1]); same_true += int(t[0] == t[1])
            errs.append(abs(int(g[0])*10 + int(g[1]) - (int(t[0])*10 + int(t[1]))))
        errs.sort()
        med = errs[len(errs)//2] if errs else -1
        print(f"\n  ==== j={j}   n={n}")
        print(f"  (a) 十位 {tens/n:.1%}   個位 {ones/n:.1%}   兩位皆對 {both/n:.1%}")
        print(f"  (b) 預測兩位相同 {same_pred/n:.1%}   （真值本身重複率 {same_true/n:.1%}）")
        print(f"  (c) |預測−真值|  中位數 {med}   平均 {sum(errs)/len(errs):.1f}   "
              f"<=1 的比例 {sum(1 for e in errs if e<=1)/len(errs):.1%}")
        out[j] = {"tens": tens/n, "ones": ones/n, "both": both/n,
                  "same_pred": same_pred/n, "same_true": same_true/n,
                  "err_median": med, "err_mean": sum(errs)/len(errs),
                  "err_le1": sum(1 for e in errs if e <= 1)/len(errs)}

    print(f"\n  判讀：")
    print(f"    十位≈個位且都低 + 預測重複率遠高於真值 → **H1 機制缺陷**（bug，非發現）")
    print(f"    十位近 100% 而個位隨機          → **解析度**問題（G7d 那條線）")
    json.dump(out, open(os.path.join(HERE, f"results_bridge_diag_digit{tag}.json"), "w"),
              indent=2)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
