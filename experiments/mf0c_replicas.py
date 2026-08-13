"""追加 `r1`／`r2` 兩份 eval replica —— **純資料步,不訓練**（Codex [181]）。

`H` audit 的 `H_g`／`H_f` 規則寫的是**三份**，而現有 artifact 只有一份
`W0|eval=300` ＋ 一份 `W1|eval=300`。**controller seed 不能冒充 `H` 的 replication**
—— `H` 不訓練，兩者是不同的東西。

保留現有 eval pair 為 `r0`；以**事前固定**的 base seed `2026081321`／`2026081331`
各生成一對 300-session `W0`／`W1` eval（`W1` 沿用 `base+1` 的既有 world-offset 規則）。
**不改**原 train、`P0` corpus、profile、query rule 或舊 artifact。
"""
import hashlib, json, os
import mf0c_data as D

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_SEEDS = {"r1": 2026081321, "r2": 2026081331}
N_SESS = 300


def main():
    old = json.load(open(os.path.join(HERE, "mf0c_artifact.json")))
    reps = {"r0": {k: old[k] for k in ("W0|eval", "W1|eval")}}
    digs = {"r0": {D.digest(s["goal"], [(e, a) for e, a, _ in s["events"]])
                   for k in ("W0|eval", "W1|eval") for s in old[k]}}
    for tag, base in BASE_SEEDS.items():
        reps[tag], digs[tag] = {}, set()
        for world, off in (("W0", 0), ("W1", 1)):        # 沿用 base+1 的 world-offset
            rows, d = D.build_mf0c(base + off, N_SESS, world)
            reps[tag][f"{world}|eval"] = rows
            digs[tag] |= set(d)

    # 與 P0 corpus、train、舊 eval 的 exact-stream digest 全不重疊
    import numpy as np
    arr = np.load(os.path.join(HERE, "mf0c_p0_corpus.npy"))
    p0 = {D.digest(int(arr[i, 0, 0]), [tuple(x) for x in arr[i, 1:]])
          for i in range(arr.shape[0])}
    tr = {D.digest(s["goal"], [(e, a) for e, a, _ in s["events"]])
          for k in ("W0|train", "W1|train") for s in old[k]}

    print("  MF0-C eval replicas（純資料步，不訓練）")
    print(f"  {'集合':>8s} {'相異 digest':>12s}")
    for t in ("r0", "r1", "r2"):
        print(f"  {t:>8s} {len(digs[t]):>12d}")
    print(f"  {'P0':>8s} {len(p0):>12d}\n  {'train':>8s} {len(tr):>12d}")

    bad = []
    names = list(digs) + ["P0", "train"]
    allsets = {**digs, "P0": p0, "train": tr}
    for i, x in enumerate(names):
        for y in names[i + 1:]:
            k = len(allsets[x] & allsets[y])
            if k:
                bad.append(f"{x}∩{y}={k}")
    print(f"\n  兩兩交集：{'全為 0 ✓' if not bad else bad}")

    path = os.path.join(HERE, "mf0c_eval_replicas.json")
    json.dump({t: reps[t] for t in ("r1", "r2")}, open(path, "w"))
    sha = D.sha_file(path)
    mp = os.path.join(HERE, "mf0c_manifest.json")
    man = json.load(open(mp))
    man["eval_replicas"] = {
        "r0": "沿用既有 mf0c_artifact.json 的 W0|eval / W1|eval",
        "r1_base_seed": BASE_SEEDS["r1"], "r2_base_seed": BASE_SEEDS["r2"],
        "world_offset_rule": "W0 = base，W1 = base+1（沿用既有規則）",
        "n_sessions": N_SESS, "path": "mf0c_eval_replicas.json", "sha": sha,
        "sha256_full": D.sha_file(path, True) if hasattr(D, "sha_file") else None,
        "digest_sizes": {t: len(digs[t]) for t in digs},
        "pairwise_intersections": bad or "all zero",
        "note": "**不改**原 train、P0 corpus、profile、query rule 或舊 artifact；只追加",
    }
    json.dump(man, open(mp, "w"), indent=2, ensure_ascii=False)
    print(f"  replicas SHA={sha}")
    print(f"  → {'PASS' if not bad else 'FAIL'}；-> mf0c_eval_replicas.json / manifest")


if __name__ == "__main__":
    main()
