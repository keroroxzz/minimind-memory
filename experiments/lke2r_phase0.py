"""`LKE-2R` **phase-0** —— 只驗 artifact 與 invariant，**不訓練任何東西**（Codex [164]）。

必須通過的 assert：

- **任一 query 都不得含 canonical name 字面**（這是 LKE-2R 的定義性條件）
- `U`／`T`／`Ø` 的 unique／tie／none 幾何逐題成立
- `T`／`Ø` 各 300 distinct，且 CAL 與 test **互斥**
- held-out frame 不引入新 lexical atom
- `T`／`Ø` 的 store **放滿該 attr 的 16 個 name**（任何 accept 必然 wrong-existing）
- `U` 的 `k* → delivery → core` ceiling：**每個 primary U cell `>= 95%`**

`T`／`Ø` **不跑 oracle core** —— 預期是零交付，跑它沒有意義。
"""
import json, os, sys
import torch
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer
import bridge_latent_schema as S
import bridge_renderer as B
import lke2r_data as D
from bridge_latent_gate import wilson
from bridge_g3c_dangling import Injector
from lke1_phase0 import oracle_cell
from bridge_train_core import DEVICE
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

HERE = os.path.dirname(os.path.abspath(__file__))


def invariants(art):
    tr = {r["q"] for r in art["train"]}
    uca = {e["q"] for e in art["u_cal"]}
    ucells = {c: {e["q"] for e in v} for c, v in art["u_cells"].items()}

    # ---- LKE-2R 的定義性條件：**name 字面完全不得出現** --------------------
    allq = list(tr | uca) + [q for s in ucells.values() for q in s]
    for t in ("to_cal", "to_test"):
        for v in art[t].values():
            allq += [e["q"] for e in v]
    for q in allq:
        toks = set(q.split())
        bad = toks & set(B.NAMES)
        assert not bad, f"query 含 canonical name 字面 {bad}：{q!r}"

    # ---- U：raw-string 互斥、pair／template leakage、邊際 -------------------
    ho = {tuple(p) for p in art["heldout_combos"]}
    assert not (tr & uca)
    for c, s in ucells.items():
        assert not (tr & s) and not (uca & s), f"U/{c} 與 train/cal 重疊"
    cs = list(ucells)
    for i in range(len(cs)):
        for j in range(i + 1, len(cs)):
            assert not (ucells[cs[i]] & ucells[cs[j]]), f"{cs[i]}/{cs[j]} 重疊"
    trp = {tuple(r["k"]) for r in art["train"]} | {tuple(e["k"]) for e in art["u_cal"]}
    assert not (trp & ho), "held-out combo 洩漏進 train/cal"
    for c in ("K", "KxP"):
        assert {tuple(e["k"]) for e in art["u_cells"][c]} <= ho
    for c in ("ID", "P"):
        assert not ({tuple(e["k"]) for e in art["u_cells"][c]} & ho)
    assert {k[0] for k in trp} == set(B.NAMES), "有 name 未在 train 出現"
    assert {k[1] for k in trp} == set(range(len(B.ATTRS))), "有 attr 未在 train 出現"
    assert D._atoms(art["heldout_frames"]) <= D._atoms(art["seen_frames"])

    # ---- 三個 stratum 的幾何：逐題查表確認 ---------------------------------
    geo = {}
    for tag, want in (("T", 2), ("O", 0)):
        for split in ("to_cal", "to_test"):
            v = art[split][tag]
            assert len(v) == art["n_to"], f"{split}/{tag} 不是 {art['n_to']} 題"
            assert len({e["q"] for e in v}) == art["n_to"], f"{split}/{tag} 有重複字串"
            for e in v:
                n = len(D.resolve_desc(e["desc"]))
                assert n == want == e["n_referents"], \
                    f"{tag} 的 {e['desc']!r} 指向 {n} 個 name，應為 {want}"
                # store 放滿該 attr 的 16 個 name → 任何 accept 必然 wrong-existing
                ks = [tuple(p) for p in e["store"]["keys"]]
                assert len(ks) == len(B.NAMES)
                assert {k[0] for k in ks} == set(B.NAMES)
                assert all(int(k[1]) == e["attr"] for k in ks)
            geo[f"{split}/{tag}"] = len(v)
        a, b = ({e["q"] for e in art["to_cal"][tag]},
                {e["q"] for e in art["to_test"][tag]})
        assert not (a & b), f"{tag} 的 CAL 與 test 重疊"
        assert not (a & tr) and not (b & tr), f"{tag} 與 train 重疊"
    # U 的 descriptor 必須恰指向 1 個 name，且就是 k* 的那個
    d_of = {v: k for k, v in D.U_DESC.items()}
    for c, v in art["u_cells"].items():
        for e in v:
            d = d_of[e["k"][0]]
            r = D.resolve_desc(d)
            assert r == [e["k"][0]], f"U/{c} 的 descriptor 不唯一：{d!r} -> {r}"
    # U 的 store 契約（沿用 LKE-1 的檢查）
    for c, v in list(art["u_cells"].items()) + [("CAL", art["u_cal"])]:
        for e in v:
            k = tuple(e["k"])
            pk = {tuple(p) for p in e["present"]["keys"]}
            assert k in pk and k not in {tuple(p) for p in e["absent"]["keys"]}
            assert len(e["present"]["keys"]) == len(e["absent"]["keys"]) == art["u_pool"]
            d2 = tuple(e["dis"])
            assert d2 in pk and d2 != k and d2[0] != k[0]
    return geo


def main(core="bridge_core_latent.pth"):
    art = D.load()
    print(f"  LKE-2R phase-0   artifact checksum={art['checksum']}")
    print("  **不訓練任何東西**；通過才寫 prereg 交 interface-only review\n")

    geo = invariants(art)
    print("  ---- 資料不變量：全部通過 ✓")
    print("    · 任一 query 都不含 canonical name 字面（LKE-2R 的定義性條件）")
    print("    · U 唯一／T 恰兩個／Ø 零個 referent，逐題查表確認")
    print("    · T／Ø 的 store 放滿該 attr 的 16 個 name → **任何 accept 必然 wrong-existing**")
    print(f"    · {geo}")
    print(f"\n  train {len(art['train'])}   u_cal {len(art['u_cal'])}"
          f"   T/Ø cal 300+300   test 300+300")

    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    blob = torch.load(os.path.join(HERE, core), map_location="cpu")
    arch = dict(blob["arch"])
    m = MiniMindForCausalLM(MiniMindConfig(vocab_size=blob["vocab"], **arch)).to(DEVICE).eval()
    m.load_state_dict(blob["model"])
    proj = S.CarrierProj(arch["hidden_size"]).to(DEVICE)
    proj.scale.fill_(blob["carrier_scale"])
    inj = Injector(proj)

    print(f"\n  ---- U 的 oracle ceiling（k* 直送，每 cell 須 >=95%）  core={core}")
    print("       T／Ø 不跑 oracle core —— 預期零交付，跑它沒有意義")
    res, fails = {}, []
    for c, v in art["u_cells"].items():
        ok, n = oracle_cell(m, tok, inj, arch["num_loops"], v)
        lo, hi = wilson(ok, n)
        if ok / n < 0.95:
            fails.append(c)
        print(f"    U/{c:>4s}  {ok}/{n} = {ok/n:>6.1%} [{lo:.0%},{hi:.0%}]"
              f"{'' if ok/n >= 0.95 else '  ✗'}")
        res[c] = {"ok": ok, "n": n}

    print()
    print(f"  → **phase-0 {'通過' if not fails else '未過'}**"
          + ("" if not fails else f"（{', '.join(fails)}）"))
    if not fails:
        print("    可寫 `LKE2R_prereg.json` 交 interface-only review；**review 前不得訓練**。")
    json.dump({"checksum": art["checksum"], "geometry": geo, "oracle": res,
               "fails": fails, "pass": not fails},
              open(os.path.join(HERE, "results_lke2r_phase0.json"), "w"),
              indent=2, ensure_ascii=False)
    print("  -> results_lke2r_phase0.json")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "bridge_core_latent.pth")
