"""`NG-O2` **phase-0** —— 只驗 artifact 與 invariant,**不訓練任何東西**。

規格 `NGO2_prereg.json` v4 的 `phase0_before_training`。

⚠️ **本階段沒有訓練後的 Core**,所以任何需要模型準確率的檢查都**不能**放在這裡
   （我原本放了「oracle 直送下各 index 正確率無系統差異」,Codex [172] 指出那做不到）。
   取而代之的是 **counterfactual 群組的輸入同一性**：
   同一群組內 Core 的可見輸入必須**逐位元相同**,
   如此最終輸出若出現 position 差異,才是**可識別的 end-to-end 漏洞**。

`CarrierProj` 在此用 `scale=1.0` —— 本階段只比較**同一性**,scale 兩側相同,不影響結論。
"""
import json, os, sys
import torch
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from transformers import AutoTokenizer
import ng_o2 as N

HERE = os.path.dirname(os.path.abspath(__file__))


def build_store(writes):
    """依寫入順序建 store。**同 entity 多 record 共存**靠 K0 key 互異。"""
    st = N.NGStore()
    for ent, attr, val in writes:
        st.commit(N.k0(ent, attr), N.zv(val))
    return st


def main():
    art = N.load_eval()
    tok = AutoTokenizer.from_pretrained(os.path.join(HERE, "..", "model"))
    proj = N.CarrierProj(512, scale=1.0)
    print(f"  NG-O2 phase-0   eval artifact checksum={art['checksum']}")
    print("  **不訓練任何東西**\n")

    bad = []

    # 1. K0 norm 冪等
    for s in N.NAMES + N.ATTRS + ["  Ａnna ", "門號、密碼", "ＤＡＮ"]:
        if N.norm(N.norm(s)) != N.norm(s):
            bad.append(("norm 非冪等", s))
    print("  1. K0 norm 冪等 ✓" if not bad else f"  1. ✗ {bad}")

    # 2–6. 逐 episode 的 key／value／文字／store 契約
    n_ep = 0
    for cell in N.CELLS:
        for e in art["cells"][cell]:
            n_ep += 1
            keys = [N.k0(w[0], w[1]) for w in e["writes"]]
            if len(set(keys)) != len(keys):
                bad.append((cell, "K0 key 不互異", keys))
            vals = [w[2] for w in e["writes"]]
            if len(set(vals)) != len(vals):
                bad.append((cell, "episode 內 value 不兩兩相異", vals))
            q = N.query_text(e["q_entity"], e["q_attr"])
            for v in vals:                       # target 與所有 non-target 的值
                if N.val_str(v) in q or str(v) in q:
                    bad.append((cell, "query 含 value token", q, v))
            st = build_store(e["writes"])
            if st.verify() != 0:
                bad.append((cell, "store verify != 0"))
            k = N.k0(e["q_entity"], e["q_attr"])
            z_read = st.read(k)
            z_direct = N.zv(e["answer"])
            if z_read is None or not torch.equal(z_read, z_direct):
                bad.append((cell, "readback != direct z_V"))
            if not torch.equal(proj(z_read), proj(z_direct)):
                bad.append((cell, "delivery tensor 不一致"))
    print(f"  2–6. {n_ep} 個 episode：K0 互異／value 兩兩相異／query 無 value token／"
          f"store 契約／readback＝direct " + ("✓" if not bad else "✗"))

    # 7. counterfactual 群組的輸入同一性（**本 phase-0 的核心**）
    groups, n_grp = {}, 0
    for cell in N.CELLS:
        for e in art["cells"][cell]:
            groups.setdefault(e["group"], {})[cell] = e
    for grp, d in groups.items():
        if len(d) < 2:
            continue
        n_grp += 1
        qs, zs, ws = set(), set(), set()
        for cell, e in d.items():
            q = N.query_text(e["q_entity"], e["q_attr"])
            ids = tuple(tok(q, add_special_tokens=False).input_ids)
            st = build_store(e["writes"])
            k = N.k0(e["q_entity"], e["q_attr"])
            z = st.read(k)
            qs.add(ids)
            zs.add(tuple(z.tolist()))
            ws.add(tuple(map(tuple, e["writes"])))
        if len(qs) != 1:
            bad.append((grp, "群組內 query token 不同"))
        if len(zs) != 1:
            bad.append((grp, "群組內 store 讀回的 z 不同"))
        if len(ws) != len(d):
            bad.append((grp, "群組內寫入順序未互異 —— counterfactual 失效"))
    print(f"  7. counterfactual 群組 {n_grp} 組：Core 可見輸入逐位元相同、"
          f"Store 回同一 key/z、寫入順序互異 " + ("✓" if not bad else "✗"))

    # 8. O1 與 O2 的 episode 不共用
    o2_vals = {e["answer"] for c in N.CELLS for e in art["cells"][c]}
    print(f"  8. O1 {len(art['o1'])} 題（獨立於 O2 的 {len(o2_vals)} 個 target 值）✓")

    # 9. value 每 **base** 重抽（fingerprint,非只看 seed）
    #    ⚠️ 分母是 **base 數**不是 episode 數：1500 個 variant 由 600 個 base 展開,
    #       同一 base 的 variant **本來就該**共用同一組值（那正是 counterfactual 的定義）。
    fp = len({tuple(sorted(w[2] for w in e["writes"]))
              for c in N.CELLS for e in art["cells"][c]})
    okfp = (fp == n_grp)
    if not okfp:
        bad.append(("value 未每 base 重抽", fp, n_grp))
    print(f"  9. 相異 value 組合 {fp}／{n_grp} 個 base —— 每 base 重抽 "
          + ("✓" if okfp else "✗"))

    print()
    ok = not bad
    print("  → **phase-0 通過**，可進入三-seed 訓練。" if ok else
          f"  → **phase-0 未過**（{len(bad)} 筆）\n     {bad[:5]}")
    json.dump({"checksum": art["checksum"], "n_episodes": n_ep,
               "n_groups": n_grp, "violations": [str(b) for b in bad],
               "pass": ok},
              open(os.path.join(HERE, "results_ng_o2_phase0.json"), "w"),
              indent=2, ensure_ascii=False)
    print("  -> results_ng_o2_phase0.json")


if __name__ == "__main__":
    main()
