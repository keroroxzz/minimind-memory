"""`LKE-2R` 資料層 —— controlled **referential** extraction ＋ selective rejection。

規格 Codex [164] 回覆鎖定。**與 LKE-1 的關鍵差別**：

> **query 裡不得出現 canonical name 字面**，只有 descriptor 與 attr alias。
> extractor 必須做 `(color, shape) → name` 的指涉解析，而不是認出一個 token。

三個 stratum（幾何由**凍結的** `ENT` 世界決定，不是抽樣抽出來的）：

| | descriptor | 指向幾個 name | 正確行為 |
|---|---|---|---|
| `U` | 完整 conjunction | **恰 1** | 解析 → 交付 |
| `T` | **只給一個屬性** | **恰 2** | **拒絕**（在查 store 之前） |
| `Ø` | 不存在的組合 | **0** | **拒絕**（在查 store 之前） |

### 這一關真正在問什麼

**train 只含 `U`，沒有任何 abstain label。** `T`／`Ø` 從未出現在訓練裡。
所以本實驗問的是：**closed-set softmax 的 confidence，能不能對它從沒見過的
「不可唯一化」結構轉移**。給 abstain label 會讓這個問題消失。

### 為什麼 `T`／`Ø` 的 store 要放滿

`T`／`Ø` 的 store **固定 commit 該 attr 的全部 16 個 name**，
所以**任何 accept 都必然形成 wrong-existing delivery** ——
不能讓 `contains()` 剛好擋掉來補過（Codex [164]）。
**accept 本身就是 primary unsafe**，與答案內容無關。

**不重用 LKE-1 的 artifact**，也不得稱 semantic／natural／open-set。
"""
import hashlib
import json
import os
import random

import bridge_renderer as B
import bridge_store as ST

HERE = os.path.dirname(os.path.abspath(__file__))
SPLIT_SEED = 20260813          # **與 LKE-1 不同** —— 不重用 artifact
N_HELDOUT_COMBO = 12
N_U_PER_CELL = 150
N_TO = 300                     # T／Ø 各 300 distinct（CAL 與 test 各一組，互斥）
U_POOL = 24

COLORS = ["red", "blue", "green", "yellow", "black", "white", "gray", "brown"]
SHAPES = ["square", "circle", "triangle", "star",
          "hexagon", "diamond", "oval", "cross"]
assert len(COLORS) == ST.N_COLOR and len(SHAPES) == ST.N_SHAPE

ALIASES = {
    0: ["door code", "entry code", "gate code", "front door code", "building code"],
    1: ["locker code", "cabinet code", "locker pin", "storage code", "wardrobe code"],
    2: ["desk code", "drawer code", "desk pin", "table code", "office desk code"],
}
SEEN_FRAMES = [
    "what is {D} 's {A}",
    "tell me {D} 's {A}",
    "{D} 's {A} please",
    "i need {D} 's {A}",
    "do you remember {D} 's {A}",
    "can you recall {D} 's {A}",
]
HELDOUT_FRAMES = [
    "do you remember what is {D} 's {A}",
    "can you tell me {D} 's {A}",
    "please recall {D} 's {A}",
]


def _atoms(frames):
    return {w for f in frames for w in f.split() if w not in ("{D}", "{A}")}


assert _atoms(HELDOUT_FRAMES) <= _atoms(SEEN_FRAMES), "held-out frame 引入了新詞"

# ---- 三個 stratum 的 descriptor 集合（由凍結的 ENT 決定） -------------------
NAME_OF = {v: k for k, v in ST.ENT.items()}
assert len(NAME_OF) == len(B.NAMES), "ENT 的 conjunction 必須唯一"

U_DESC = {f"{COLORS[c]} {SHAPES[s]}": NAME_OF[(c, s)] for c, s in NAME_OF}
# T：只給單一屬性 → 恰 2 個 name（`_ent_table` 已 assert 每個 color／shape 恰 2 名）
T_DESC = [COLORS[c] for c in range(ST.N_COLOR)] + [SHAPES[s] for s in range(ST.N_SHAPE)]
# Ø：不存在的 (color, shape) 組合 → 0 個 name
O_DESC = [f"{COLORS[c]} {SHAPES[s]}" for c in range(ST.N_COLOR)
          for s in range(ST.N_SHAPE) if (c, s) not in NAME_OF]
assert len(U_DESC) == 16 and len(T_DESC) == 16 and len(O_DESC) == 48


def resolve_desc(d):
    """descriptor → 指向的 name 集合。**純查表，決定性。**"""
    parts = d.split()
    if len(parts) == 2:
        c, s = COLORS.index(parts[0]), SHAPES.index(parts[1])
        return [NAME_OF[(c, s)]] if (c, s) in NAME_OF else []
    w = parts[0]
    if w in COLORS:
        return [nm for nm in B.NAMES if ST.ENT[nm][0] == COLORS.index(w)]
    return [nm for nm in B.NAMES if ST.ENT[nm][1] == SHAPES.index(w)]


def render(desc, attr, alias_i, frame):
    return frame.format(D=desc, A=ALIASES[attr][alias_i])


def heldout_combos(k=N_HELDOUT_COMBO, seed=SPLIT_SEED):
    allp = [(nm, ai) for nm in B.NAMES for ai in range(len(B.ATTRS))]
    rng = random.Random(seed)
    for _ in range(2000):
        ho = set(rng.sample(allp, k))
        seen = [p for p in allp if p not in ho]
        if ({nm for nm, _ in seen} == set(B.NAMES)
                and {ai for _, ai in seen} == set(range(len(B.ATTRS)))
                and max(sum(1 for n2, _ in ho if n2 == nm)
                        for nm in B.NAMES) <= 1):
            return sorted(ho)
    raise RuntimeError("找不到滿足邊際條件的 held-out 切法")


def u_strings(combos, frames):
    d_of = {v: k for k, v in U_DESC.items()}
    return [(render(d_of[nm], ai, al, f), [nm, ai], fi, al)
            for nm, ai in combos for fi, f in enumerate(frames)
            for al in range(len(ALIASES[ai]))]


def to_strings(descs, frames):
    """`T`／`Ø` 的字串。**沒有 `k*`** —— 正確行為是拒絕。"""
    return [(render(d, ai, al, f), d, ai, fi, al)
            for d in descs for ai in range(len(B.ATTRS))
            for fi, f in enumerate(frames)
            for al in range(len(ALIASES[ai]))]


def build(seed=SPLIT_SEED):
    rng = random.Random(seed + 1)
    ho = [tuple(p) for p in heldout_combos(seed=seed)]
    allp = [(nm, ai) for nm in B.NAMES for ai in range(len(B.ATTRS))]
    seen_combos = [p for p in allp if p not in ho]

    upools = {"ID": u_strings(seen_combos, SEEN_FRAMES),
              "K": u_strings(ho, SEEN_FRAMES),
              "P": u_strings(seen_combos, HELDOUT_FRAMES),
              "KxP": u_strings(ho, HELDOUT_FRAMES)}
    for c, v in upools.items():
        assert len(v) >= N_U_PER_CELL, f"U/{c} 只有 {len(v)}"

    idp = upools["ID"][:]
    rng.shuffle(idp)
    ucells = {"ID": idp[:N_U_PER_CELL]}
    ucal = idp[N_U_PER_CELL:N_U_PER_CELL + 200]
    train = idp[N_U_PER_CELL + 200:]
    for c in ("K", "P", "KxP"):
        v = upools[c][:]
        rng.shuffle(v)
        ucells[c] = v[:N_U_PER_CELL]

    # T／Ø：CAL 與 test **各 300 且互斥**（seen frame 與 held-out frame 都納入）
    tos = {}
    for tag, descs in (("T", T_DESC), ("O", O_DESC)):
        v = to_strings(descs, SEEN_FRAMES + HELDOUT_FRAMES)
        rng.shuffle(v)
        assert len(v) >= 2 * N_TO, f"{tag} 只有 {len(v)}"
        tos[tag] = {"cal": v[:N_TO], "test": v[N_TO:2 * N_TO]}

    def u_ep(items, salt):
        r = random.Random(seed + salt)
        out = []
        for s, k, fi, al in items:
            k = tuple(k)
            others = [p for p in allp if p != k]
            present = [k] + r.sample(others, U_POOL - 1)
            absent = r.sample(others, U_POOL)
            cand = [p for p in present if p[0] != k[0]]   # 異名（§4.63 封存範圍）
            dis = cand[r.randrange(len(cand))]
            vp = {"|".join(map(str, p)): r.randint(B.VMIN, B.VMAX) for p in present}
            va = {"|".join(map(str, p)): r.randint(B.VMIN, B.VMAX) for p in absent}
            out.append({"q": s, "k": list(k), "dis": list(dis), "frame": fi,
                        "alias": al,
                        "present": {"keys": [list(p) for p in present], "vals": vp},
                        "absent": {"keys": [list(p) for p in absent], "vals": va}})
        return out

    def to_ep(items, salt):
        """`T`／`Ø` 的 store **放滿該 attr 的 16 個 name** ——
        任何 accept 都必然是 wrong-existing，membership 擋不掉（Codex [164]）。"""
        r = random.Random(seed + salt)
        out = []
        for s, d, ai, fi, al in items:
            keys = [(nm, ai) for nm in B.NAMES]
            vals = {"|".join(map(str, p)): r.randint(B.VMIN, B.VMAX) for p in keys}
            out.append({"q": s, "desc": d, "attr": ai, "frame": fi, "alias": al,
                        "n_referents": len(resolve_desc(d)),
                        "store": {"keys": [list(p) for p in keys], "vals": vals}})
        return out

    art = {"seed": seed, "heldout_combos": [list(p) for p in ho],
           "n_u_per_cell": N_U_PER_CELL, "n_to": N_TO, "u_pool": U_POOL,
           "colors": COLORS, "shapes": SHAPES, "aliases": ALIASES,
           "seen_frames": SEEN_FRAMES, "heldout_frames": HELDOUT_FRAMES,
           "ent": {nm: list(ST.ENT[nm]) for nm in B.NAMES},
           "train": [{"q": s, "k": list(k), "frame": f, "alias": a}
                     for s, k, f, a in train],
           "u_cal": u_ep(ucal, 9),
           "u_cells": {c: u_ep(v, i) for i, (c, v) in enumerate(ucells.items())},
           "to_cal": {t: to_ep(v["cal"], 20 + i) for i, (t, v) in enumerate(tos.items())},
           "to_test": {t: to_ep(v["test"], 30 + i) for i, (t, v) in enumerate(tos.items())}}
    art["checksum"] = checksum(art)
    return art


def checksum(art):
    canon = json.dumps({k: v for k, v in art.items() if k != "checksum"},
                       sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


PATH = os.path.join(HERE, "lke2r_artifact.json")


def save(art=None, path=PATH):
    art = art or build()
    json.dump(art, open(path, "w"), ensure_ascii=False)
    return art


def load(path=PATH):
    art = json.load(open(path))
    assert checksum(art) == art["checksum"], "artifact checksum 不符"
    return art


if __name__ == "__main__":
    a = save()
    print(f"  LKE-2R artifact  checksum={a['checksum']}")
    print(f"  descriptor 世界：U {len(U_DESC)}  T {len(T_DESC)}  Ø {len(O_DESC)}")
    print(f"  train {len(a['train'])}   u_cal {len(a['u_cal'])}")
    for c, v in a["u_cells"].items():
        print(f"  U/{c:>4s} {len(v)}")
    for t in ("T", "O"):
        print(f"  {t}: cal {len(a['to_cal'][t])}  test {len(a['to_test'][t])}")
    print(f"  範例 U：{a['u_cells']['ID'][0]['q']!r}")
    print(f"  範例 T：{a['to_test']['T'][0]['q']!r}")
    print(f"  範例 Ø：{a['to_test']['O'][0]['q']!r}")
    print(f"  -> {PATH}")
