"""`LKE-1` **phase-0** —— 只驗 artifact 與 invariant，**不訓練任何東西**（Codex [161]）。

兩件事：

1. **資料不變量**：train／cal／四個 cell 之間**無 raw-string overlap**、
   **無 template／pair leakage**、membership 恰 50:50，並報每 cell 的
   distinct query／key support。
2. **oracle ceiling**：用**隱藏的** `k*` 直接走 `Store → delivery → core`，
   每 cell `A_ans >= 95%`。**先過這關才准讀 extractor** ——
   否則 extractor 的分數會被下游天花板汙染，歸因就散掉了。

phase-0 過了才寫 `LKE1_prereg.json` 交 interface-only review；
**review 通過前不得訓練。**
"""
import json, os, sys
import torch
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer
import bridge_latent_schema as S
import bridge_renderer as B
import bridge_store as ST
import lke_data as D
from bridge_latent_gate import gen, wilson
from bridge_g3c_dangling import Injector
from bridge_train_core import DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))


def invariants(art):
    """全部用 assert —— **任何一條不過就不准往下走**。"""
    tr = {r["q"] for r in art["train"]}
    ca = {r["q"] for r in art["cal"]}
    cells = {c: {e["q"] for e in v} for c, v in art["cells"].items()}
    ho = {tuple(p) for p in art["heldout_combos"]}

    assert not (tr & ca), f"train/cal raw-string 重疊 {len(tr & ca)}"
    for c, s in cells.items():
        assert not (tr & s), f"train/{c} raw-string 重疊 {len(tr & s)}"
        assert not (ca & s), f"cal/{c} raw-string 重疊 {len(ca & s)}"
    for a, b in (("ID", "K"), ("ID", "P"), ("ID", "KxP"),
                 ("K", "P"), ("K", "KxP"), ("P", "KxP")):
        assert not (cells[a] & cells[b]), f"{a}/{b} raw-string 重疊"

    # pair leakage：held-out combo 不得出現在 train/cal
    tr_pairs = {tuple(r["k"]) for r in art["train"]} | {tuple(r["k"]) for r in art["cal"]}
    assert not (tr_pairs & ho), f"held-out combo 洩漏進 train/cal：{tr_pairs & ho}"
    for c in ("K", "KxP"):
        ks = {tuple(e["k"]) for e in art["cells"][c]}
        assert ks <= ho, f"{c} 含非 held-out 的 combo"
    for c in ("ID", "P"):
        ks = {tuple(e["k"]) for e in art["cells"][c]}
        assert not (ks & ho), f"{c} 含 held-out 的 combo"

    # template leakage：held-out frame 不得出現在 train/cal/ID/K
    nf = len(art["seen_frames"])
    tr_f = {r["frame"] for r in art["train"]} | {r["frame"] for r in art["cal"]}
    assert max(tr_f) < nf, "train/cal 用到 held-out frame"
    for c in ("P", "KxP"):
        assert all(e["frame"] < len(art["heldout_frames"]) for e in art["cells"][c])
    # 邊際：每個 name／attr 都必須在 train 見過
    assert {k[0] for k in tr_pairs} == set(B.NAMES), "有 name 未在 train 出現"
    assert {k[1] for k in tr_pairs} == set(range(len(B.ATTRS))), "有 attr 未在 train 出現"
    # lexical atom：held-out frame 一個新詞都不得引入
    assert D._atoms(art["heldout_frames"]) <= D._atoms(art["seen_frames"])

    # membership 恰 50:50，以及 active distractor 的規則
    # **CAL 與 test cell 走完全相同的檢查** —— tau 的選擇條件就是量在 CAL 上，
    # 若 CAL 沒有凍結的 store，threshold protocol 不封閉（Codex [162] review 的 blocker）。
    for c, v in list(art["cells"].items()) + [("CAL", art["cal"])]:
        for e in v:
            k = tuple(e["k"])
            pk = {tuple(p) for p in e["present"]["keys"]}
            assert k in pk, f"{c} present 不含 k*"
            assert k not in {tuple(p) for p in e["absent"]["keys"]}, f"{c} absent 含 k*"
            assert len(e["present"]["keys"]) == len(e["absent"]["keys"]) == art["pool"]
            d = tuple(e["dis"])
            assert d in pk, f"{c} 的 dis 不在 present store 裡"
            assert d != k, f"{c} 的 dis 等於 target"
            assert d[0] != k[0], f"{c} 的 dis 與 target 同名（§4.63 已封存的範圍）"
    sup = {c: {"distinct_q": len(s),
                "distinct_k": len({tuple(e["k"]) for e in art["cells"][c]}),
                "distinct_frame": len({e["frame"] for e in art["cells"][c]}),
                "distinct_alias": len({e["alias"] for e in art["cells"][c]})}
           for c, s in cells.items()}
    sup["CAL"] = {"distinct_q": len(ca),
                  "distinct_k": len({tuple(e["k"]) for e in art["cal"]}),
                  "distinct_frame": len({e["frame"] for e in art["cal"]}),
                  "distinct_alias": len({e["alias"] for e in art["cal"]}),
                  "episodes": 2 * len(art["cal"])}
    return sup


def mk_store(spec):
    st = ST.Store()
    for p in spec["keys"]:
        st.commit(p[0], int(p[1]), spec["vals"]["|".join(map(str, p))])
    assert st.verify() == 0
    return st


@torch.no_grad()
def oracle_cell(m, tok, inj, loops, entries):
    """用**隱藏的** `k*` 直接走 Store→delivery→core。這是 extractor 的天花板。"""
    ok = 0
    for e in entries:
        k = (e["k"][0], int(e["k"][1]))
        st = mk_store(e["present"])
        z, status = ST.retrieve(st, *k)
        assert status == "ok", "present store 應該一定讀得到 k*"
        dis = (e["dis"][0], int(e["dis"][1]))       # **凍結在 artifact 裡**，不在此處挑
        assert dis[0] != k[0], "distractor 不得與 target 同名（§4.63 已封存的範圍）"
        tv = e["present"]["vals"]["|".join(map(str, e["k"]))]
        dv = e["present"]["vals"]["|".join((dis[0], str(dis[1])))]
        facts = [(k[0], k[1], tv), (dis[0], dis[1], dv)]
        ep = B.Episode(facts, f"{k[0]} {B.ATTRS[k[1]]} 是 多少", B.val_str(tv), [0], 0)
        mc = inj(torch.stack([z, st.read(*dis)]))
        ids = torch.tensor(tok(tok.bos_token + S.render_latent(ep)[0],
                               add_special_tokens=False).input_ids)
        ok += int(gen(m, tok, ids, loops, mc) == ep.answer)
    return ok, len(entries)


def main(core="bridge_core_latent.pth"):
    art = D.load()
    print(f"  LKE-1 phase-0   artifact checksum={art['checksum']}")
    print("  **不訓練任何東西**；phase-0 過了才寫 prereg 交 interface-only review\n")

    sup = invariants(art)
    print("  ---- 資料不變量：全部通過 ✓")
    print(f"  {'cell':>5s} {'distinct q':>11s} {'distinct k':>11s} "
          f"{'frames':>7s} {'aliases':>8s}")
    for c, v in sup.items():
        print(f"  {c:>5s} {v['distinct_q']:>11d} {v['distinct_k']:>11d} "
              f"{v['distinct_frame']:>7d} {v['distinct_alias']:>8d}")
    print(f"\n  train {len(art['train'])}   cal {len(art['cal'])} query"
          f" → **{2*len(art['cal'])} 個 CAL episode**（50:50，store 已凍結）"
          f"   held-out combos {len(art['heldout_combos'])}/48")

    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    blob = torch.load(os.path.join(HERE, core), map_location="cpu")
    arch = dict(blob["arch"])
    m = MiniMindForCausalLM(MiniMindConfig(vocab_size=blob["vocab"], **arch)).to(DEVICE).eval()
    m.load_state_dict(blob["model"])
    proj = S.CarrierProj(arch["hidden_size"]).to(DEVICE)
    proj.scale.fill_(blob["carrier_scale"])
    inj = Injector(proj)

    print(f"\n  ---- oracle ceiling（k* 直送，**每 cell 須 >=95%**）  core={core}")
    res, fails = {}, []
    for c, v in art["cells"].items():
        ok, n = oracle_cell(m, tok, inj, arch["num_loops"], v)
        lo, hi = wilson(ok, n)
        if ok / n < 0.95:
            fails.append(c)
        print(f"    {c:>5s}  {ok}/{n} = {ok/n:>6.1%} [{lo:.0%},{hi:.0%}]"
              f"{'' if ok/n >= 0.95 else '  ✗'}")
        res[c] = {"ok": ok, "n": n}

    print()
    if fails:
        print(f"  → **phase-0 未過**（oracle ceiling: {', '.join(fails)}）")
        print("    下游天花板不足 —— **不得**在此基礎上讀 extractor 分數，")
        print("    否則 extraction 的失敗會與 delivery 的失敗混在一起。")
    else:
        print("  → **phase-0 通過**：資料不變量成立，oracle ceiling 四 cell 皆 >=95%。")
        print("    可以寫 `LKE1_prereg.json` 交 interface-only review；**review 前不得訓練**。")
    json.dump({"checksum": art["checksum"], "support": sup,
               "oracle": res, "fails": fails, "pass": not fails},
              open(os.path.join(HERE, "results_lke1_phase0.json"), "w"),
              indent=2, ensure_ascii=False)
    print("  -> results_lke1_phase0.json")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "bridge_core_latent.pth")
