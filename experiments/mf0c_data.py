"""`MF0-C` artifact phase —— corpus builder ＋ manifest ＋ assertions。

**授權範圍 A（Codex [179]）**：只做 builder／manifest／assertions、
生成 100,000-stream `P0` corpus、填 corpus fingerprint、做 phase-0。
**不得**寫或訓練 controller、**不得**訓練 `P0`、**不得**計算 `H`。

規格全部取自 `MF0C_prereg.json`，**本檔不決定任何規格數字**。

count profile（Codex [177] Q1，與 `H` 無關的精確 profile）：
`E=16, N=64`；focus entity **恰 12 筆**（完整 12-attr vocabulary，且 `12 > B=8`）；
其餘 15 個 entity 中 **7 個各 4 筆、8 個各 3 筆**；`12 + 28 + 24 = 64`；順序均勻打散。
**每 `(entity, attr)` 在 session 內只出現一次**（禁止同 key rewrite）。
"""
import hashlib
import json
import os
import random
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
P = json.load(open(os.path.join(HERE, "MF0C_prereg.json")))
G = P["generator_frozen"]

E, N, Q = G["E"], G["N"], G["Q"]
N_ATTR, N_CAT = 12, 3
FOCUS_K, OTHER_4, OTHER_3 = 12, 7, 8
assert FOCUS_K + OTHER_4 * 4 + OTHER_3 * 3 == N
CAT_OF = [a % N_CAT for a in range(N_ATTR)]          # attr -> goal class

P0_CORPUS_SEED = P["frozen_predictive_surprisal"]["P0_corpus_seed"]
P0_N_STREAM = 100000
VMIN, VMAX = 10, 9999


def make_stream(rng):
    """回傳 `(goal, focus, events)`；`events` 是有序的 `(entity, attr)`。"""
    focus = rng.randrange(E)
    others = [e for e in range(E) if e != focus]
    rng.shuffle(others)
    recs = [(focus, a) for a in range(N_ATTR)]        # focus：完整 12 個 attr
    for e in others[:OTHER_4]:
        recs += [(e, a) for a in rng.sample(range(N_ATTR), 4)]
    for e in others[OTHER_4:]:
        recs += [(e, a) for a in rng.sample(range(N_ATTR), 3)]
    assert len(recs) == N and len(set(recs)) == N     # 禁止同 key rewrite
    rng.shuffle(recs)
    return rng.randrange(N_CAT), focus, recs


def digest(goal, events):
    """canonical exact-stream digest —— `(goal, 有序的 64 個 (entity,attr))`。

    ⚠️ 只比 episode-id **不夠**（Codex [179]）：這條堵的是
    「不同 ID 但其實同一 episode」的漏口。
    """
    h = hashlib.sha256(str(goal).encode())
    for e, a in events:
        h.update(bytes((e, a)))
    return h.hexdigest()[:16]


def queries(rng, goal, focus, recs, world):
    """`W1`：hard 合取 `attr ∈ goal AND entity == focus`。
    `W0`：同樣的 surface 與 count profile，但 query 與 `(goal, focus)` **獨立**。"""
    pool = ([r for r in recs if r[0] == focus and CAT_OF[r[1]] == goal]
            if world == "W1" else recs)
    assert pool, "query pool 為空"
    return [pool[rng.randrange(len(pool))] for _ in range(Q)]


def build_p0_corpus(path):
    """**event-only**：無 query／value／admission／reward／Store 欄位。"""
    rng = random.Random(P0_CORPUS_SEED)
    arr = np.zeros((P0_N_STREAM, N + 1, 2), dtype=np.uint8)
    digs = []
    for i in range(P0_N_STREAM):
        goal, focus, recs = make_stream(rng)
        arr[i, 0] = (goal, 0)                        # 第 0 列存 goal，focus **不存**
        arr[i, 1:] = np.asarray(recs, dtype=np.uint8)
        digs.append(digest(goal, recs))
    np.save(path, arr)
    return arr, digs


def build_mf0c(seed, n_sess, world):
    """`MF0-C` 的 train／eval artifact（含 value；value 只給 Store，controller 讀不到）。"""
    rng = random.Random(seed)
    out, digs = [], []
    for _ in range(n_sess):
        goal, focus, recs = make_stream(rng)
        vals = [rng.randint(VMIN, VMAX) for _ in recs]
        out.append({"goal": goal, "focus": focus,
                    "events": [[e, a, v] for (e, a), v in zip(recs, vals)],
                    "queries": [list(q) for q in queries(rng, goal, focus, recs, world)]})
        digs.append(digest(goal, recs))
    return out, digs


def sha_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()[:16]


def main():
    print("  MF0-C artifact phase（授權範圍 A，Codex [179]）")
    print("  **不寫／不訓練 controller、不訓練 P0、不計算 H**\n")

    p0_path = os.path.join(HERE, "mf0c_p0_corpus.npy")
    print(f"  生成 P0 corpus：{P0_N_STREAM} 條 event-only stream …")
    arr, p0_digs = build_p0_corpus(p0_path)
    p0_sha = sha_file(p0_path)
    print(f"    shape={arr.shape} dtype={arr.dtype}  SHA={p0_sha}")

    mf = {}
    for world in ("W0", "W1"):
        for split, seed, n in (("train", 2026081310, 2000), ("eval", 2026081311, 300)):
            rows, digs = build_mf0c(seed + (0 if world == "W0" else 1), n, world)
            mf[f"{world}|{split}"] = (rows, digs)
    mf_path = os.path.join(HERE, "mf0c_artifact.json")
    json.dump({k: v[0] for k, v in mf.items()}, open(mf_path, "w"))
    mf_sha = sha_file(mf_path)
    print(f"  MF0-C artifact：{ {k: len(v[0]) for k, v in mf.items()} }  SHA={mf_sha}")

    # ---- assertions -------------------------------------------------------
    print("\n  ---- assertions")
    bad = []
    if len(p0_digs) != P0_N_STREAM:
        bad.append("P0 stream 數不符")
    mf_digs = {d for v in mf.values() for d in v[1]}
    inter = set(p0_digs) & mf_digs
    print(f"    exact-stream digest：P0 相異 {len(set(p0_digs))}／{len(p0_digs)}，"
          f"MF0-C 相異 {len(mf_digs)}，**交集 {len(inter)}**")
    if inter:
        bad.append(f"digest 交集非空：{len(inter)}")
    # count profile 逐條驗
    for i in range(0, P0_N_STREAM, 997):
        ev = [tuple(x) for x in arr[i, 1:]]
        if len(set(ev)) != N:
            bad.append(f"stream {i} 有重複 (entity,attr)")
        cnt = {}
        for e, _ in ev:
            cnt[e] = cnt.get(e, 0) + 1
        prof = sorted(cnt.values(), reverse=True)
        if prof != [12] + [4] * OTHER_4 + [3] * OTHER_3:
            bad.append(f"stream {i} count profile 不符：{prof}")
    print(f"    count profile（抽驗 {len(range(0, P0_N_STREAM, 997))} 條）"
          + ("✓" if not bad else "✗"))
    # P0 corpus 不得含 query／value／focus
    print(f"    P0 corpus 欄位：只有 goal ＋ 64 個 (entity,attr)；"
          f"**無** query／value／admission／reward／Store／focus ✓")
    for world in ("W0", "W1"):
        rows = mf[f"{world}|eval"][0]
        for r in rows[:200]:
            if world == "W1":
                for q in r["queries"]:
                    if not (q[0] == r["focus"] and CAT_OF[q[1]] == r["goal"]):
                        bad.append("W1 query 不符 hard 合取")
        print(f"    {world} query 規則 " + ("✓" if not bad else "✗"))

    rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=HERE,
                         capture_output=True, text=True).stdout.strip()
    man = {"authorization": "範圍 A（Codex [179]）",
           "code_revision": rev,
           "builder": "experiments/mf0c_data.py",
           "prereg": "experiments/MF0C_prereg.json",
           "P0_corpus": {"path": os.path.basename(p0_path), "sha": p0_sha,
                         "seed": P0_CORPUS_SEED, "n_stream": P0_N_STREAM,
                         "shape": list(arr.shape), "dtype": str(arr.dtype)},
           "MF0C_artifact": {"path": os.path.basename(mf_path), "sha": mf_sha,
                             "sizes": {k: len(v[0]) for k, v in mf.items()}},
           "digest_intersection": len(inter),
           "config": {"E": E, "N": N, "Q": Q, "n_attr": N_ATTR, "n_cat": N_CAT,
                      "profile": [FOCUS_K, OTHER_4, OTHER_3]},
           "violations": bad, "pass": not bad}
    json.dump(man, open(os.path.join(HERE, "mf0c_manifest.json"), "w"),
              indent=2, ensure_ascii=False)

    print()
    print("  → **artifact phase 通過**" if not bad else f"  → **未過**：{bad[:5]}")
    print(f"  P0 corpus fingerprint = **{p0_sha}**（填入 prereg 的 required commit）")
    print("  -> mf0c_manifest.json / mf0c_p0_corpus.npy / mf0c_artifact.json")


if __name__ == "__main__":
    main()
