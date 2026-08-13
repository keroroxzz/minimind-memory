"""`MF0-C` **v2 artifact** —— 修 train→eval leakage（Codex [182]）。**純資料步,不訓練。**

- 舊 `mf0c_artifact.json` **位元不動**；另存 **append-only** 的 invalidation report。
- v2 **只重生兩個 train world**（base `2026081340`）；`P0`、`r0/r1/r2` eval **保持原檔**。
- 必須 assert 並記錄**完整交集矩陣**：任兩個跨 partition 的 canonical-stream digest 集合皆為 0
  （`W0 train` 雖不進 optimizer，**也要查**）。

⚠️「全域互斥」指 **event-stream generator artifacts**；
   controller-init／index／action RNG 是**另列 namespace**，數字碰巧相同不構成資料獨立性證據。
"""
import hashlib, itertools, json, os
import numpy as np
import mf0c_data as D

HERE = os.path.dirname(os.path.abspath(__file__))
TRAIN_BASE_V2 = 2026081340
N_TRAIN, N_EVAL = 2000, 300


def dig_of(sessions):
    return {D.digest(s["goal"], [(e, a) for e, a, _ in s["events"]]) for s in sessions}


def main():
    old_path = os.path.join(HERE, "mf0c_artifact.json")
    old = json.load(open(old_path))
    old_sha = D.sha_file(old_path)

    # ---- append-only invalidation report（舊檔位元不動）---------------------
    inv_path = os.path.join(HERE, "mf0c_invalidation.jsonl")
    rec = {"ts": "2026-08-13", "invalidated": "mf0c_artifact.json",
           "old_sha": old_sha, "old_sha256_full": D.sha_file(old_path, True),
           "finding": "train→eval event-stream leakage：W1|train ∩ r0|W0 = 300",
           "root_cause": "seed 撞號 —— build_mf0c(seed + (0 if W0 else 1))，"
                         "train base 2026081310 的 W1 用 2026081311，"
                         "而 eval base 正是 2026081311；兩個 world 的 query 抽樣次數相同，"
                         "rng 演進一致 → 同一批 stream，只有 query 不同",
           "how_found": "Codex [181] 要求的 replica exact-stream digest 交集檢查；"
                        "我先前的 artifact phase 只查 P0∩MF0C，未查 train∩eval",
           "invalid_for": ["H audit", "W0 leakage check", "所有 controller 結論"],
           "unaffected": ["P0 corpus", "P0 checkpoint", "r0/r1/r2 eval"],
           "replacement": "mf0c_artifact_v2.json（train base 2026081340）"}
    with open(inv_path, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ---- v2：只重生 train --------------------------------------------------
    v2 = {}
    for world, off in (("W0", 0), ("W1", 1)):
        rows, _ = D.build_mf0c(TRAIN_BASE_V2 + off, N_TRAIN, world)
        v2[f"{world}|train"] = rows
    v2_path = os.path.join(HERE, "mf0c_artifact_v2.json")
    json.dump(v2, open(v2_path, "w"))
    v2_sha = D.sha_file(v2_path)

    # ---- 完整交集矩陣 ------------------------------------------------------
    parts = {"P0": None, "train|W0": dig_of(v2["W0|train"]),
             "train|W1": dig_of(v2["W1|train"])}
    arr = np.load(os.path.join(HERE, "mf0c_p0_corpus.npy"))
    parts["P0"] = {D.digest(int(arr[i, 0, 0]), [tuple(x) for x in arr[i, 1:]])
                   for i in range(arr.shape[0])}
    reps = json.load(open(os.path.join(HERE, "mf0c_eval_replicas.json")))
    for w in ("W0", "W1"):
        parts[f"r0|{w}"] = dig_of(old[f"{w}|eval"])
        for t in ("r1", "r2"):
            parts[f"{t}|{w}"] = dig_of(reps[t][f"{w}|eval"])

    print("  MF0-C v2 artifact（純資料步，不訓練）")
    print(f"  舊 artifact SHA={old_sha} → **INVALID**，已寫入 append-only report")
    print(f"  v2 train base={TRAIN_BASE_V2}（W0=…40 / W1=…41），SHA={v2_sha}\n")
    print(f"  {'partition':>12s} {'相異 digest':>12s}")
    for k, v in parts.items():
        print(f"  {k:>12s} {len(v):>12d}")

    bad = []
    for x, y in itertools.combinations(parts, 2):
        n = len(parts[x] & parts[y])
        if n:
            bad.append(f"{x}∩{y}={n}")
    print(f"\n  完整交集矩陣（{len(list(itertools.combinations(parts,2)))} 對）："
          + ("**全為 0** ✓" if not bad else f"✗ {bad}"))

    man = {"authorization": "Codex [182]：v2 artifact 修復（純資料步）",
           "invalidated": {"file": "mf0c_artifact.json", "sha": old_sha,
                           "report": "mf0c_invalidation.jsonl"},
           "artifact_v2": {"path": "mf0c_artifact_v2.json", "sha": v2_sha,
                           "sha256_full": D.sha_file(v2_path, True),
                           "train_base_seed": TRAIN_BASE_V2,
                           "sizes": {k: len(v) for k, v in v2.items()},
                           "note": "**只含 train**；P0 與 r0/r1/r2 eval 保持原檔"},
           "index_schedule": {"namespace": "v2", "seed": 2026081391,
                              "note": "改號是為避免再以 …1311 造成審計混淆"},
           "intersection_matrix": {"partitions": {k: len(v) for k, v in parts.items()},
                                   "n_pairs": len(list(itertools.combinations(parts, 2))),
                                   "violations": bad or "all zero"},
           "namespace_note": "『全域互斥』僅指 event-stream generator artifacts；"
                             "controller-init／index／action RNG 為另列 namespace",
           "pass": not bad}
    json.dump(man, open(os.path.join(HERE, "mf0c_manifest_v2.json"), "w"),
              indent=2, ensure_ascii=False)
    print(f"\n  → **v2 {'PASS' if not bad else 'FAIL'}**"
          f"；所有 H／controller **僅可讀 v2**")
    print("  -> mf0c_artifact_v2.json / mf0c_manifest_v2.json / mf0c_invalidation.jsonl")


if __name__ == "__main__":
    main()
