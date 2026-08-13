"""`RWA-0` artifact **v2** —— 補齊 negative 的 render（Codex [191]）。**純資料步，不訓練。**

**唯一補件**：為 target ＋ 三 negative 的 **1,200 個既存 key 各**凍結 `WH` 與 `RH` surface
（共 **2,400** eval surfaces）。
**不可**重抽／重排 anchor、改 seed、改 template、改 gate。

舊 artifact／manifest **位元保留**，另加 append-only `INCOMPLETE` report。
"""
import itertools, json, os
from collections import Counter
import rwa0_data as B

HERE = os.path.dirname(os.path.abspath(__file__))


def prog_sig(fn):
    """renderer program 的簽章：把 alias 位置換成 slot 名。"""
    t = (0, 1, 2, 3)
    slot = {B.W[0]: "x0", B.W[1]: "x1", B.W[2]: "x2", B.W[3]: "x3",
            B.R[0]: "x0", B.R[1]: "x1", B.R[2]: "x2", B.R[3]: "x3"}
    return tuple(slot.get(tok, tok) for tok in fn(t))


def main():
    print("  RWA-0 artifact v2（補 negative render；**不訓練**）")
    old_path = os.path.join(HERE, "rwa0_artifact.json")
    old = json.load(open(old_path))
    train, ev, reserved = B.build()

    # 同一性：v2 必須與 v1 的 anchor／train **逐筆相同**
    assert [r["t"] for r in ev] == [r["t"] for r in old["eval"]], "anchor 不同"
    assert [r["e"] for r in ev] == [r["e"] for r in old["eval"]], "e 不同"
    assert [r["t"] for r in train] == [r["t"] for r in old["train"]], "train 不同"
    print("  ✓ 300 anchor／`e`／train 與 v1 逐筆相同（未重抽、未重排）")

    # ---- 唯一補件：1,200 個 key 各凍結 WH 與 RH -----------------------------
    ev2 = []
    for r in ev:
        keys = [tuple(r["t"])] + [tuple(n) for n in r["negs"]]
        slots = ["target", "same_entity", "same_attr", "swap"]
        ev2.append({**r, "surfaces": [
            {"slot": s, "key": list(k), "K": list(B.K(k)),
             "w": B.WH(k), "r": B.RH(k)} for s, k in zip(slots, keys)]})
    n_surf = sum(len(x["surfaces"]) * 2 for x in ev2)
    print(f"  ✓ 補齊 {sum(len(x['surfaces']) for x in ev2)} 個 key 的 WH／RH"
          f" → **{n_surf} 條 eval surface**")

    bad = []
    # ① renderer-program 的 train–eval 交集（此前**未** assert）
    tr_p = {prog_sig(f) for _, f, _, g in B.CELLS for f in (f,)} | \
           {prog_sig(g) for _, _, _, g in B.CELLS}
    ev_p = {prog_sig(B.WH), prog_sig(B.RH)}
    ip = tr_p & ev_p
    if ip:
        bad.append(f"renderer-program 交集 {len(ip)}")
    print(f"  ① renderer-program 的 train–eval 交集 = {len(ip)}"
          f"（train {len(tr_p)} 種、eval {len(ev_p)} 種）" + ("✓" if not ip else "✗"))

    # ② full-string 交集與 injectivity **擴至全部 2,400 條**
    tr_s = {" ".join(r["w"]) for r in train} | {" ".join(r["r"]) for r in train}
    ev_s, inj = set(), True
    s2k = {}
    for x in ev2:
        for s in x["surfaces"]:
            for txt in (" ".join(s["w"]), " ".join(s["r"])):
                ev_s.add(txt)
                if s2k.setdefault(txt, tuple(s["K"])) != tuple(s["K"]):
                    inj = False
    for r in train:
        for txt in (" ".join(r["w"]), " ".join(r["r"])):
            if s2k.setdefault(txt, tuple(r["K"])) != tuple(r["K"]):
                inj = False
    isx = tr_s & ev_s
    if isx: bad.append(f"full-string 交集 {len(isx)}")
    if not inj: bad.append("injectivity 失敗")
    print(f"  ② full-string 交集 = {len(isx)}（eval 相異 {len(ev_s)} 條）；"
          f"injectivity " + ("✓" if inj and not isx else "✗"))

    # ③ 1,200 key pairwise distinct 且與 train disjoint
    ek = [tuple(s["key"]) for x in ev2 for s in x["surfaces"]]
    tk = {tuple(r["t"]) for r in train}
    if len(set(ek)) != 1200: bad.append(f"eval key 不是 1200 相異：{len(set(ek))}")
    if set(ek) & tk: bad.append(f"eval∩train key {len(set(ek)&tk)}")
    print(f"  ③ eval key 相異 {len(set(ek))}／1200；與 train 交集 "
          f"{len(set(ek)&tk)} " + ("✓" if len(set(ek))==1200 and not (set(ek)&tk) else "✗"))

    # ④ 每個實際出現的 eval alias 的一個 train-surface witness
    ev_al = {t for x in ev2 for s in x["surfaces"] for t in s["w"]+s["r"]
             if t in B.W or t in B.R}
    wit, miss = {}, []
    for al in sorted(ev_al):
        for r in train:
            if al in r["w"]: wit[al] = " ".join(r["w"]); break
            if al in r["r"]: wit[al] = " ".join(r["r"]); break
        else: miss.append(al)
    if miss: bad.append(f"{len(miss)} 個 eval alias 無 train witness")
    print(f"  ④ {len(ev_al)} 個實際出現的 eval alias 皆有 train witness "
          + ("✓" if not miss else f"✗ 缺 {miss[:5]}"))

    art = {"train": train, "eval": ev2}
    fp = B.sha(art)
    json.dump(art, open(os.path.join(HERE, "rwa0_artifact_v2.json"), "w"))
    with open(os.path.join(HERE, "rwa0_invalidation.jsonl"), "a") as f:
        f.write(json.dumps({"ts": "2026-08-13", "file": "rwa0_artifact.json",
            "status": "INCOMPLETE（非 INVALID）", "fingerprint": "7b4919bbeee2c3c8",
            "gap": "eval 只凍結 300 個 target 的 WH/RH；三種 negative 只有 raw tuple／negK，"
                   "無任何 write/read surface → (b) 三 strata 無輸入可測、"
                   "(c) 無 negative write carrier；injectivity／full-string 只覆蓋 target",
            "found_by": "Codex [191] 檢查 phase-0 implementation",
            "note": "已跑的七項 assertion 結果保留且成立；本缺口是 artifact representation，非 gate 放寬",
            "replacement": "rwa0_artifact_v2.json"}, ensure_ascii=False) + "\n")
    man = {"authorization": "Codex [191]：additive phase-0；**未授權訓練**",
           "supersedes": {"file": "rwa0_artifact.json", "fingerprint": "7b4919bbeee2c3c8",
                          "status": "INCOMPLETE", "report": "rwa0_invalidation.jsonl"},
           "artifact_v2_fingerprint": fp,
           "identical_to_v1": ["300 anchors", "e", "reserved key-set", "train data", "prereg v3"],
           "only_addition": "1,200 個 key 各凍結 WH 與 RH → 2,400 eval surfaces",
           "checks": {"renderer_program_intersection": len(ip),
                      "full_string_intersection": len(isx), "injectivity": inj,
                      "eval_keys_distinct": len(set(ek)), "eval_train_key_intersection":
                      len(set(ek) & tk), "alias_witness_missing": miss},
           "violations": bad, "pass": not bad}
    json.dump(man, open(os.path.join(HERE, "rwa0_manifest_v2.json"), "w"),
              indent=2, ensure_ascii=False)
    print(f"\n  → **v2 phase-0 {'PASS' if not bad else 'FAIL'}**"
          + ("" if not bad else f"：{bad[:4]}"))
    print(f"  artifact v2 fingerprint = **{fp}**")


if __name__ == "__main__":
    main()
