"""殘餘失敗是「精度不足」還是「同時交付多條記憶時的串擾」？

`nfreq=8` 之後的誤差是**雙峰**的：j=0 的 |誤差| 中位數 0、平均 13.9 ——
大多數完全正確，少數錯得離譜。精度問題不會有這種長尾；它看起來是**整題答錯**。

而且十位 62.0% 與個位 58.7% 已經**一樣爛**，不再有頻率特異性 —— 所以卡住
60% 的原因與解析度無關，Fourier 已經把解析度那一層修掉了。

可證偽的預測（H2 串擾）：每題有 2..4 條 fact，除了 `force_used` 之外還有 60%
機率變成載體，所以常常**同時**有多條 latent 在不同位置被送進去。若失敗來自
載體之間的串擾：

    正確率隨**該題的載體數**單調下降，且**1 個載體時接近 oracle**。

若正確率與載體數無關而平坦在 ~60%，H2 被推翻，殘餘失敗是別的東西。

⚠️ 這是**看到結果之後追加的 post-hoc 診斷**，不是預先登記的 primary。
   它的作用是**指出下一個實驗**，不是宣告結論。（Codex #126 立的規矩：
   事後分析必須標明，不得冒充 prereg。）

另外分開報 `n_carrier` 與 `n_used`（這題答案真正需要幾條）：
若下降只跟 `n_used` 走，那是**融合**的負擔；若跟 `n_carrier` 走（包含答案
用不到的那些），那才是真正的**串擾** —— 用不到的記憶不該有任何代價。
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
from bridge_delivery import Delivery, greedy, make_item
from bridge_train_core import DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))


@torch.no_grad()
def main(tag="_f8", n=600):
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    blob = torch.load(os.path.join(HERE, "bridge_core.pth"), map_location="cpu")
    arch = dict(blob["arch"])
    cfg = MiniMindConfig(vocab_size=blob["vocab"], **arch)
    m = MiniMindForCausalLM(cfg).to(DEVICE).eval()
    m.load_state_dict(blob["model"])

    d = torch.load(os.path.join(HERE, f"bridge_delivery{tag}.pth"), map_location="cpu")
    loops, nl = d["loops"], arch["num_hidden_layers"] * arch["num_loops"]
    # ⚠️ `mode` 一定要從 checkpoint 帶進來。少了它會預設 mode="kv"，
    #    而 v_only/k_only 的權重**形狀相同、載得進去**，於是靜默地套用一條
    #    訓練時被歸零、從未學過的 ΔK/ΔV —— 評估結果全錯而且沒有任何錯誤訊息。
    #    （`_f8` 本身就是 kv，所以先前用本檔跑出的 _f8 數字不受影響。）
    dl = Delivery(B.LATENT_DIM, nl, arch["num_key_value_heads"],
                  arch["hidden_size"] // arch["num_attention_heads"],
                  nfreq=d.get("nfreq", 0), mode=d.get("mode", "kv")).to(DEVICE)
    dl.load_state_dict(d["delivery"]); dl.eval()
    print(f"  {tag}  nfreq={d.get('nfreq', 0)}  mode={d.get('mode', 'kv')}  "
          f"num_loops={loops}   n={n}/j")
    print(f"  ⚠️ post-hoc 診斷（看到結果後追加），用途是指出下一個實驗，不是結論\n")

    out = {}
    for j in (0, 1):
        by_c, by_u = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
        rng = random.Random(777 + j)
        for _ in range(n):
            ep, mask, ids, pos, Z, M = make_item(tok, rng, j)
            ok = int(greedy(m, tok, ids, loops, dl, Z, M, nl) == ep.answer)
            nc = sum(mask)
            nu = sum(1 for i in ep.ask_idx if mask[i])   # 答案真正需要的載體數
            by_c[nc][0] += ok; by_c[nc][1] += 1
            by_u[nu][0] += ok; by_u[nu][1] += 1
        print(f"  ==== j={j}")
        print(f"    依**載體總數** n_carrier（含答案用不到的）：")
        for k in sorted(by_c):
            o, t = by_c[k]
            print(f"      n_carrier={k}   {o/t:6.1%}   (n={t})")
        print(f"    依**答案需要的載體數** n_used：")
        for k in sorted(by_u):
            o, t = by_u[k]
            print(f"      n_used   ={k}   {o/t:6.1%}   (n={t})")
        out[j] = {"by_carrier": {str(k): by_c[k] for k in by_c},
                  "by_used": {str(k): by_u[k] for k in by_u}}
        print()

    print("  判讀：")
    print("    隨 n_carrier 單調下降、n_carrier=1 接近 oracle → **H2 串擾**")
    print("      （用不到的記憶也造成代價，這是真正的干擾）")
    print("    只隨 n_used 下降、n_carrier 無關                → **融合負擔**，非串擾")
    print("    兩者都平坦在 ~60%                              → H2 推翻，另尋原因")
    json.dump(out, open(os.path.join(HERE, f"results_bridge_diag_ncarrier{tag}.json"),
                        "w"), indent=2)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "_f8")
