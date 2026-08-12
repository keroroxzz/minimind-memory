"""`LKE-2R` 三-seed train/eval —— **一次跑完，跑完直接照 prereg 判讀**。

授權：Codex [165]（artifact checksum `639e74973dc153f4`）。

**train 只含 `U`，沒有任何 abstain label** —— `T`／`Ø` 從未出現在訓練裡。
本實驗問的是 closed-set softmax confidence 能否對未見的「不可唯一化」結構轉移。

`tau`：每 seed 用其自己的 extractor ＋ frozen CAL，選**最低**的 grid 點，
使 **`T` 與 `Ø` 各自** `0/300` confidence accept；無可行者則 `tau=1`（全拒），
`U` 的 utility gate 自然判 FAIL。

**任何 confidence accept 本身就是 primary unsafe**，與 membership、與答案內容無關。
診斷欄另報 `accepted_total / same_attr_wrong_existing / wrong_attr_guarded_absent`
（Codex [165] 的措辭修正）—— 三者**不得**改變 `0/300` accept 主閘。

`u_cal` 僅作 U 的 calibration diagnostic，**不得**加入 tau tie-break。
"""
import json, os, sys
import torch
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer
import bridge_latent_schema as S
import bridge_renderer as B
import lke2r_data as D
from bridge_g3c_dangling import Injector, cp_upper
from lke1_phase0 import mk_store
from lke1_run import Extractor, predict, train as train_extractor
from lke1_run import run_cell as u_run_cell
from bridge_train_core import DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))
P = json.load(open(os.path.join(HERE, "LKE2R_prereg.json")))
# `train_extractor` 來自 `lke1_run`，它讀的是 **LKE1** 的 prereg。
# 兩份規格目前逐欄相同（Codex [164] 指定沿用），但若日後有人改動其一，
# 這裡必須大聲失敗，而不是靜默用到 LKE-1 的數字。
import lke1_run as _L1
assert _L1.O == P["optimizer"] and _L1.E == P["learner"]["encoder"], \
    "LKE-2R 的 learner/optimizer 與 lke1_run 載入的不一致"


def to_accepts(m, tok, entries, tau, inj=None):
    """`T`／`Ø` 的評估。回傳主閘欄位 ＋ 三個診斷欄。

    **正確行為是拒絕**，所以這裡不該有任何 injector 呼叫；
    `inj` 傳進來只是為了證明它的計數沒有增加。
    """
    preds = predict(m, tok, [e["q"] for e in entries])
    r = {"accepted": 0, "n": len(entries), "delivered": 0,
         "same_attr_wrong_existing": 0, "wrong_attr_guarded_absent": 0}
    c0 = inj.calls if inj else 0
    for e, (k_hat, c) in zip(entries, preds):
        if c < tau:
            continue                                  # 正確：拒絕
        r["accepted"] += 1                            # **這一格本身就是 primary unsafe**
        st = mk_store(e["store"])
        if st.contains(*k_hat):
            r["same_attr_wrong_existing"] += 1        # 交付了別人的記憶
            r["delivered"] += 1
        else:
            r["wrong_attr_guarded_absent"] += 1       # attr 也錯 → guard 擋下（診斷用）
    if inj:
        assert inj.calls == c0, "T／Ø 路徑不得有任何交付"
    return r


def pick_tau(m, tok, art):
    """最低的 grid 點，使 **T 與 Ø 各自** 0/300 accept。無可行者 → tau=1。"""
    for tau in P["selective_policy"]["tau_grid"]:
        rt = to_accepts(m, tok, art["to_cal"]["T"], tau)
        ro = to_accepts(m, tok, art["to_cal"]["O"], tau)
        if rt["accepted"] == 0 and ro["accepted"] == 0:
            return tau, rt, ro
    tau = P["selective_policy"]["tau_fallback"]
    return tau, to_accepts(m, tok, art["to_cal"]["T"], tau), \
        to_accepts(m, tok, art["to_cal"]["O"], tau)


def main():
    art = D.load()
    assert art["checksum"] == P["artifact"]["checksum"], "artifact 不是授權的那份"
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    blob = torch.load(os.path.join(HERE, "bridge_core_latent.pth"), map_location="cpu")
    arch = dict(blob["arch"])
    core = MiniMindForCausalLM(MiniMindConfig(vocab_size=blob["vocab"],
                                              **arch)).to(DEVICE).eval()
    core.load_state_dict(blob["model"])
    proj = S.CarrierProj(arch["hidden_size"]).to(DEVICE)
    proj.scale.fill_(blob["carrier_scale"])
    inj = Injector(proj)
    loops = arch["num_loops"]

    print(f"  LKE-2R 三-seed run   artifact={art['checksum']}（Codex [165] 授權）")
    print("  **train 只含 U，不給 T／Ø 任何 abstain label**")
    print("  問：closed-set softmax confidence 能否對未見的『不可唯一化』結構轉移\n")

    out, fails = {}, []
    for seed in P["seeds"]:
        print(f"  ==== seed {seed}")
        m = train_extractor(tok, art, seed)
        tau, ct, co = pick_tau(m, tok, art)
        fb = tau >= 1.0
        print(f"    tau={tau}（CAL accept：T {ct['accepted']}/300、"
              f"Ø {co['accepted']}/300）{'  ⚠️ fallback 全拒' if fb else ''}")
        out[str(seed)] = {"tau": tau, "cal": {"T": ct, "O": co},
                          "u": {}, "to": {}}

        print(f"    {'U cell':>7s} {'raw exact':>11s} {'交付答對':>11s} "
              f"{'false_ab':>9s} {'unsafe':>8s} {'逐位元同':>9s}")
        for c, v in art["u_cells"].items():
            r = u_run_cell(core, tok, inj, loops, m, v, tau)
            re_ = r["raw_exact"] / r["n_raw"]
            dv = r["present_deliver_ok"] / max(r["present_deliver_n"], 1)
            fa = r["false_abstain"] / r["n_raw"]
            ok = (re_ >= 0.95 and dv >= 0.95 and fa <= 0.05 and r["unsafe"] == 0
                  and r["bitwise_same"] == r["present_deliver_n"])
            if not ok:
                fails.append(f"seed{seed}/U-{c}")
            ucol = "{}/{}".format(r["unsafe"], r["n_ep"])
            print(f"    {c:>7s} {re_:>10.1%} {dv:>10.1%} {fa:>8.1%} "
                  f"{ucol:>8s}" + ("" if ok else "  ✗"))
            out[str(seed)]["u"][c] = r

        print(f"    {'':>7s} {'accept':>11s} {'交付':>11s} "
              f"{'同attr wrong-exist':>19s} {'異attr 被guard擋':>17s}")
        for tag in ("T", "O"):
            r = to_accepts(m, tok, art["to_test"][tag], tau, inj)
            ok = r["accepted"] == 0 and r["delivered"] == 0
            if not ok:
                fails.append(f"seed{seed}/{tag}")
            ub = cp_upper(r["accepted"], r["n"])
            acol = "{}/{}".format(r["accepted"], r["n"])
            dcol = "{}/{}".format(r["delivered"], r["n"])
            print(f"    {tag:>7s} {acol:>11s} {dcol:>11s} "
                  f"{r['same_attr_wrong_existing']:>19d} "
                  f"{r['wrong_attr_guarded_absent']:>17d}  UB {ub:.2%}"
                  f"{'' if ok else '  ✗'}")
            out[str(seed)]["to"][tag] = r

    print()
    if fails:
        print(f"  → **LKE-2R FAIL**（{', '.join(fails)}）")
        print("    依 prereg：seal LKE-2R，結論僅拆成 U extraction／")
        print("    selective-confidence transfer／downstream ceiling 三者之一，")
        print("    然後轉 exact-key conflict/overwrite。**不得**加 typo 配方或改 grid 去救。")
    else:
        print("  → **LKE-2R PASS**（三 seed × 全部格）。")
        print("    只稱「此有限受控 descriptor grammar 下的 unique extraction 與")
        print("    calibrated abstention」；**不升格** natural／semantic。")
    json.dump({"artifact": art["checksum"], "seeds": out, "fails": fails,
               "pass": not fails},
              open(os.path.join(HERE, "results_lke2r.json"), "w"),
              indent=2, ensure_ascii=False)
    print("  -> results_lke2r.json")


if __name__ == "__main__":
    main()
