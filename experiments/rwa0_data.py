"""`RWA-0` artifact ＋ phase-0 —— **授權範圍：只產生一次 frozen artifact 並跑 assertions**。

Codex [190]：**未授權** model training／training smoke／改超參數／觀察任何 learned metric。
規格全部取自 `RWA0_prereg.json` v3，**本檔不決定任何規格數字**。
"""
import hashlib, itertools, json, os, random
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
P = json.load(open(os.path.join(HERE, "RWA0_prereg.json")))
DS = P["data_split_locked"]

D = list(range(12))
PAIRS = [(i, j) for i in D for j in D if i != j]        # 字典序 → rank 0..131
RANK = {p: k for k, p in enumerate(PAIRS)}
assert len(PAIRS) == 132

W = [f"w{i}" for i in D]
R = [f"r{i}" for i in D]
VOCAB = ["PAD", "memo", "find", "E0", "E1", "A0", "A1"] + W + R
assert len(set(W) & set(R)) == 0 and len(VOCAB) == 7 + 24


def K(t):
    """`(x0,x1,x2,x3) -> (e_rank, a_rank)`。"""
    return (RANK[(t[0], t[1])], RANK[(t[2], t[3])])


# ---- 四條 train program ＋ 兩條 held-out（exact string，Codex [188] #1）------
def W0(t): return ["memo", "E0", W[t[0]], "E1", W[t[1]], "A0", W[t[2]], "A1", W[t[3]]]
def W1(t): return ["memo", "A1", W[t[3]], "A0", W[t[2]], "E1", W[t[1]], "E0", W[t[0]]]
def R0(t): return ["find", "A0", R[t[2]], "A1", R[t[3]], "E0", R[t[0]], "E1", R[t[1]]]
def R1(t): return ["find", "E1", R[t[1]], "E0", R[t[0]], "A1", R[t[3]], "A0", R[t[2]]]
def WH(t): return ["memo", "E1", W[t[1]], "A0", W[t[2]], "E0", W[t[0]], "A1", W[t[3]]]
def RH(t): return ["find", "A1", R[t[3]], "E0", R[t[0]], "A0", R[t[2]], "E1", R[t[1]]]


CELLS = [("W0", W0, "R0", R0), ("W0", W0, "R1", R1),
         ("W1", W1, "R0", R0), ("W1", W1, "R1", R1)]


def negs(t, e):
    """三個 negative：same-entity／same-attr／binding-swap（**構造決定**）。"""
    a, b, c, d = t
    return [(a, b, c, e), (a, e, c, d), (a, c, b, d)]


def reserve_eval():
    """五元組 permutation → 貪婪收下 target＋三 negative 全未被收下者，取首 300。"""
    five = [x for x in itertools.permutations(D, 5)]
    random.Random(DS["eval_seed"]).shuffle(five)
    taken, anchors = set(), []
    for a, b, c, d, e in five:
        t = (a, b, c, d)
        cand = [t] + negs(t, e)
        if any(k in taken for k in cand) or len(set(cand)) != 4:
            continue
        taken.update(cand)
        anchors.append({"t": list(t), "e": e, "negs": [list(x) for x in negs(t, e)]})
        if len(anchors) == DS["eval_anchors"]:
            break
    return anchors, taken


def build():
    anchors, reserved = reserve_eval()
    if len(anchors) < DS["eval_anchors"]:
        raise SystemExit(f"phase-0 FAIL：eval reservation 只湊到 {len(anchors)}")
    rng = random.Random(DS["train_seed"])
    all4 = [t for t in itertools.permutations(D, 4) if t not in reserved]
    train = []
    for wi, wf, ri, rf in CELLS:
        for _ in range(DS["train_anchors"] // 4):
            t = all4[rng.randrange(len(all4))]        # with replacement
            train.append({"t": list(t), "cell": f"{wi}-{ri}",
                          "w": wf(t), "r": rf(t), "K": list(K(t))})
    ev = [{**a, "w": WH(tuple(a["t"])), "r": RH(tuple(a["t"])),
           "K": list(K(tuple(a["t"]))),
           "negK": [list(K(tuple(n))) for n in a["negs"]]} for a in anchors]
    return train, ev, reserved


def sha(o):
    return hashlib.sha256(json.dumps(o, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()[:16]


def main():
    print("  RWA-0 artifact ＋ phase-0（Codex [190] 授權；**未授權訓練**）")
    train, ev, reserved = build()
    print(f"  train {len(train)} draws → {2*len(train)} surfaces；eval {len(ev)} anchors\n")
    bad = []

    tr_atoms = Counter(tok for r in train for tok in r["w"] + r["r"])
    for al in W + R:
        if tr_atoms[al] == 0:
            bad.append(f"alias {al} 未在 train 出現")
    print(f"  1. 12 個 W ＋ 12 個 R alias 各至少出現 1 次 "
          + ("✓" if not bad else "✗"))

    ecls = Counter(r["K"][0] for r in train); acls = Counter(r["K"][1] for r in train)
    miss = [c for c in range(132) if ecls[c] == 0] + [c for c in range(132) if acls[c] == 0]
    if miss:
        bad.append(f"{len(miss)} 個 class 未在 train 出現")
    print(f"  2. 132 entity ＋ 132 attr class 各至少出現 1 次 "
          + ("✓" if not miss else f"✗ 缺 {len(miss)}"))

    tr_s = {" ".join(r["w"]) for r in train} | {" ".join(r["r"]) for r in train}
    ev_s = {" ".join(r["w"]) for r in ev} | {" ".join(r["r"]) for r in ev}
    inter_s = tr_s & ev_s
    if inter_s:
        bad.append(f"完整 string 交集 {len(inter_s)}")
    print(f"  3. 完整 surface string 的 train–eval 交集 = {len(inter_s)} "
          + ("✓" if not inter_s else "✗"))

    surf2K = {}
    inj = True
    for r in train + ev:
        for s in (" ".join(r["w"]), " ".join(r["r"])):
            k = tuple(r["K"])
            if surf2K.setdefault(s, k) != k:
                inj = False
    if not inj:
        bad.append("renderer 非單射：同一 surface 對到兩個 K*")
    print(f"  4. renderer injectivity（surface → K* 唯一）" + ("✓" if inj else "✗"))

    tr_k = {tuple(r["t"]) for r in train}
    ev_k = {tuple(r["t"]) for r in ev} | {tuple(n) for r in ev for n in r["negs"]}
    inter_k = tr_k & ev_k
    if inter_k:
        bad.append(f"key-set 交集 {len(inter_k)}")
    print(f"  5. 1,200 eval key 與 train key 的 exact 交集 = {len(inter_k)} "
          f"（eval keys {len(ev_k)}）" + ("✓" if not inter_k else "✗"))

    nb = 0
    for r in ev:
        for nk in r["negK"]:
            if nk == r["K"]:
                nb += 1
        sw = r["negs"][2]
        if Counter(sw) != Counter(r["t"]):
            nb += 1
    if nb:
        bad.append(f"negative 檢查失敗 {nb}")
    print(f"  6. 三 negative 的 `K* != K_anchor` ＋ binding-swap multiset 相等 "
          + ("✓" if not nb else f"✗ {nb}"))

    # ---- 7. 以新 RWAKey 重跑四條 Store contract ----------------------------
    class RWAStore:
        def __init__(self): self.d = {}
        def commit(self, k, v):
            if k in self.d and self.d[k] != v:
                return "reject_conflict"
            if k in self.d:
                return "duplicate"
            self.d[k] = v; return "new"
        def contains(self, k): return k in self.d
        def read(self, k): return self.d.get(k)
    st = RWAStore()
    k1, k2 = (0, 1, 2, 3), (0, 1, 2, 4)
    c = []
    c.append(st.commit(k1, "v1") == "new")
    c.append(st.contains(k1) and not st.contains(k2))          # exact membership
    c.append(st.commit(k1, "v1") == "duplicate")               # idempotence
    c.append(st.commit(k1, "v2") == "reject_conflict" and st.read(k1) == "v1")
    c.append(st.read(k2) is None)                              # badread → fail-closed
    if not all(c):
        bad.append(f"Store contract {c}")
    print(f"  7. 新 `RWAKey` 的四條 Store contract（membership／idempotence／"
          f"conflict-reject／fail-closed）" + ("✓" if all(c) else "✗"))

    art = {"train": train, "eval": ev}
    fp = sha(art)
    path = os.path.join(HERE, "rwa0_artifact.json")
    json.dump(art, open(path, "w"))
    man = {"authorization": "Codex [190] 範圍：artifact phase-0；**未授權訓練**",
           "prereg_sha": sha(P), "artifact_fingerprint": fp,
           "sizes": {"train_draws": len(train), "train_surfaces": 2 * len(train),
                     "eval_anchors": len(ev), "reserved_keys": len(reserved)},
           "seeds": {"train": DS["train_seed"], "eval": DS["eval_seed"]},
           "intersections": {"full_string": len(inter_s), "key_set": len(inter_k)},
           "violations": bad, "pass": not bad}
    json.dump(man, open(os.path.join(HERE, "rwa0_manifest.json"), "w"),
              indent=2, ensure_ascii=False)
    print()
    print(f"  → **phase-0 {'PASS' if not bad else 'FAIL'}**"
          + ("" if not bad else f"：{bad[:4]}"))
    print(f"  artifact fingerprint = **{fp}**")
    print("  -> rwa0_artifact.json / rwa0_manifest.json")


if __name__ == "__main__":
    main()
