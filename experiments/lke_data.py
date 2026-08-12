"""`LKE-1` 資料層 —— **受控語言**的 query 表面 → 既有 canonical `(name, attr)`。

規格 Codex [161] 回覆鎖定。**先講清楚這是什麼、不是什麼**：

> 這是 **learned controlled-language canonicalization**，
> **不是**自然語言、**不是** open-set identity、**不是** write-side extraction。
> 真實文本必須另立有雙標註／adjudication 的 data protocol，
> **不得用 LKE-1 的分數冒充**。

### ground truth 怎麼來

生成器持有隱藏 intent tuple `k* = (name, attr)`，由它產生受控英語句法，
再**凍結**成 train/cal/test artifact（原始字串、`k*`、template/alias family、
membership、checksum）。**不用 LLM／web text，不做事後人工判句。**

### 兩個正交的 OOD 軸

1. **held-out key combination**：12/48 個 `(name, attr)` 不進 train，
   但**每個 name 與每個 attr 的邊際仍在 train 出現過**。
2. **held-out frame family**：未見過的 query frame，
   但**只重排既有 lexical atom** —— 一個新詞都不引入。
   這是硬約束：若引入新詞，測到的就是 OOV 而不是結構泛化。

### 四個 surface cell

`ID`（見過的 combo × 見過的 frame）、`K`（held-out combo）、
`P`（held-out frame）、`K×P`（兩者同時）。每 cell 150 個**相異** query string。
"""
import hashlib
import json
import os
import random

import bridge_renderer as B

HERE = os.path.dirname(os.path.abspath(__file__))
SPLIT_SEED = 20260812
N_HELDOUT_COMBO = 12
N_PER_CELL = 150
POOL = 24

# ---- 屬性的語義詞彙 ------------------------------------------------------
# `a/b/c` 不再是裸符號，而是有意義的英文短語 —— 這是 LKE-1 與前面所有實驗的差別。
# 每個屬性 5 個 alias，**全部都在 train 出現**（Codex：所有 lexical atom 都須在 train 見過）。
ALIASES = {
    0: ["door code", "entry code", "gate code", "front door code", "building code"],
    1: ["locker code", "cabinet code", "locker pin", "storage code", "wardrobe code"],
    2: ["desk code", "drawer code", "desk pin", "table code", "office desk code"],
}

# ---- frame 文法 ----------------------------------------------------------
# `{N}` = name，`{A}` = attr alias。
SEEN_FRAMES = [
    "what is {N} 's {A}",
    "tell me {N} 's {A}",
    "{N} 's {A} please",
    "i need {N} 's {A}",
    "do you remember {N} 's {A}",
    "can you recall {N} 's {A}",
]

# ⚠️ **held-out frame 只重排既有 atom，不引入任何新詞。**
#    下面每個詞都出現在 `SEEN_FRAMES` 裡：do you remember what is can tell me please recall
HELDOUT_FRAMES = [
    "do you remember what is {N} 's {A}",
    "can you tell me {N} 's {A}",
    "please recall {N} 's {A}",
]


def _atoms(frames):
    return {w for f in frames for w in f.split() if w not in ("{N}", "{A}")}


assert _atoms(HELDOUT_FRAMES) <= _atoms(SEEN_FRAMES), \
    "held-out frame 引入了新詞 —— 那會測成 OOV，不是結構泛化"


def heldout_combos(k=N_HELDOUT_COMBO, seed=SPLIT_SEED):
    """抽 `k` 個 `(name, attr)` 不進 train。**每個 name／attr 的邊際仍須見過。**"""
    allp = [(nm, ai) for nm in B.NAMES for ai in range(len(B.ATTRS))]
    rng = random.Random(seed)
    for _ in range(1000):
        ho = set(rng.sample(allp, k))
        seen = [p for p in allp if p not in ho]
        if ({nm for nm, _ in seen} == set(B.NAMES)
                and {ai for _, ai in seen} == set(range(len(B.ATTRS)))):
            # 每個 name 最多抽走 1 個組合 —— 否則某些 name 的支撐會過度變薄
            if max(sum(1 for n2, _ in ho if n2 == nm) for nm in B.NAMES) <= 1:
                return sorted(ho)
    raise RuntimeError("找不到滿足邊際條件的 held-out 切法")


def render(name, attr, alias_i, frame):
    return frame.format(N=name, A=ALIASES[attr][alias_i])


def all_strings(combos, frames):
    """`(query 字串, k*, frame index, alias index)` 的完整笛卡兒積。"""
    out = []
    for nm, ai in combos:
        for fi, f in enumerate(frames):
            for al in range(len(ALIASES[ai])):
                out.append((render(nm, ai, al, f), [nm, ai], fi, al))
    return out


def build(seed=SPLIT_SEED):
    """回傳凍結的 artifact。**同一 seed 逐位元可重現**，checksum 隨附。"""
    rng = random.Random(seed + 1)
    ho = [tuple(p) for p in heldout_combos(seed=seed)]
    allp = [(nm, ai) for nm in B.NAMES for ai in range(len(B.ATTRS))]
    seen_combos = [p for p in allp if p not in ho]

    pools = {
        "ID": all_strings(seen_combos, SEEN_FRAMES),
        "K": all_strings(ho, SEEN_FRAMES),
        "P": all_strings(seen_combos, HELDOUT_FRAMES),
        "KxP": all_strings(ho, HELDOUT_FRAMES),
    }
    for c, v in pools.items():
        assert len(v) >= N_PER_CELL, f"{c} 只有 {len(v)} 個相異字串，不足 {N_PER_CELL}"

    # ID 池切成 test / cal / train **互斥**，其餘三 cell 全部是 test-only
    idp = pools["ID"][:]
    rng.shuffle(idp)
    cells = {"ID": idp[:N_PER_CELL]}
    cal_items = idp[N_PER_CELL:N_PER_CELL + 200]
    train = idp[N_PER_CELL + 200:]
    for c in ("K", "P", "KxP"):
        v = pools[c][:]
        rng.shuffle(v)
        cells[c] = v[:N_PER_CELL]

    # 每個 query 配一個 present store 與一個 absent store（pool=24，50:50）
    def stores_for(items, salt):
        """`dis` = 要與 target 一起注入的那條 distractor，**寫進凍結 artifact**。

        ⚠️ **必須排除同名。** `pool=24` 從 48 個組合抽，必然含與 target 同名的條目；
           而 §4.63 已封存「不涵蓋同實體多屬性」，§4.55 的 core 在同名 distractor
           上只有 31.5%。若隨手挑到同名，等於在 LKE-1 裡偷測一個已知失敗的能力，
           oracle ceiling 會被它拉低而 extraction 的歸因就散了。
           Codex [161] 明列 LKE-1 **不測**同實體多屬性 —— 這是實作該範圍，不是調參。
           同名以外**不再篩選**（同 attr／異 attr 都留著），否則就變成挑好看的配置。
        """
        r = random.Random(seed + salt)
        out = []
        for s, k, fi, al in items:
            k = tuple(k)
            others = [p for p in allp if p != k]
            present = [k] + r.sample(others, POOL - 1)
            absent = r.sample(others, POOL)
            cand = [p for p in present if p[0] != k[0]]
            assert cand, "present store 裡找不到異名 distractor"
            dis = cand[r.randrange(len(cand))]
            vals_p = {"|".join(map(str, p)): r.randint(B.VMIN, B.VMAX) for p in present}
            vals_a = {"|".join(map(str, p)): r.randint(B.VMIN, B.VMAX) for p in absent}
            out.append({"q": s, "k": list(k), "dis": list(dis),
                        "frame": fi, "alias": al,
                        "present": {"keys": [list(p) for p in present], "vals": vals_p},
                        "absent": {"keys": [list(p) for p in absent], "vals": vals_a}})
        return out

    art = {"seed": seed, "heldout_combos": [list(p) for p in ho],
           "n_per_cell": N_PER_CELL, "pool": POOL,
           "aliases": ALIASES, "seen_frames": SEEN_FRAMES,
           "heldout_frames": HELDOUT_FRAMES,
           "train": [{"q": s, "k": list(k), "frame": f, "alias": a}
                     for s, k, f, a in train],
           # ⚠️ CAL **必須**帶凍結的 store —— tau 的選擇條件是 CAL 上的
           #    wrong-existing delivery rate，若在跑的時候才用 seed 動態造 store，
           #    threshold protocol 就不封閉、也不可 checksum（Codex [162] review 的 blocker）。
           #    200 個 CAL query × (present, absent) = **400 個 CAL episode**，50:50。
           "cal": stores_for(cal_items, 4),
           "cells": {c: stores_for(v, i) for i, (c, v) in enumerate(cells.items())}}
    art["checksum"] = checksum(art)
    return art


def checksum(art):
    """canonical 串流的 SHA —— 任何一格改動都會變。**不含 checksum 欄本身。**"""
    canon = json.dumps({k: v for k, v in art.items() if k != "checksum"},
                       sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


PATH = os.path.join(HERE, "lke1_artifact.json")


def save(art=None, path=PATH):
    art = art or build()
    json.dump(art, open(path, "w"), ensure_ascii=False)
    return art


def load(path=PATH):
    art = json.load(open(path))
    assert checksum(art) == art["checksum"], "artifact checksum 不符 —— 檔案被改過"
    return art


if __name__ == "__main__":
    a = save()
    print(f"  LKE-1 artifact  checksum={a['checksum']}")
    print(f"  held-out combos {len(a['heldout_combos'])}/48   pool={a['pool']}")
    print(f"  train {len(a['train'])}   cal {len(a['cal'])}")
    for c, v in a["cells"].items():
        print(f"  cell {c:>4s}  {len(v)} 個相異 query")
    print(f"  -> {PATH}")
