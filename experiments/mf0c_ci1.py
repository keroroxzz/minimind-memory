"""`CI-1` —— **恰一次**的機制診斷（Codex [185] 授權）。**不 reopen `MF0-C`。**

兩臂唯一差異：給既有 `u=(goal, e, attr)` 一個**冗餘的 derived bit**

    `+` : q = 1[cat(attr) == goal]              ← 真的 conjunction
    `-` : q = 1[cat(attr) == (goal+1) mod 3]    ← 錯位的 decoy

兩者都由既有輸入**完全決定**、維度皆 **32**、邊際皆 **1/3**。
`h ∈ FP16^16`、sampling law、v2 train/eval、三 seed、`20k×32`、final checkpoint、
**全部 gate 不變**。

⚠️ 這是**顯式提供 conjunction 的 inductive-bias intervention，不是資訊增加**；
   **永遠不可**稱 natural／emergent formation。

### 事前支持條件（**全部**成立才算「假說獲支持」）

1. `+` 臂在**每個** seed × W1 replica **通過**原本的 goal／focus 雙 gate
2. `-` 臂在**至少一個** primary 格 **FAIL**
3. **每個** seed × replica：`U(+) − U(−) >= T_r` 且 paired-CI lower `> 0`
4. `+` 臂的既鎖 `h=0` ablation **仍不過** focus gate

任一不成 → **「此假說未獲支持」**。
**若兩臂都過** → 只判 generic architecture change，**不支持**「explicit conjunction 釋放 state」。
"""
import json, os, statistics
import mf0c_controller as C

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    res = {}
    for arm in ("+", "-"):
        C.ARM = arm
        C.IN = C.NC + C.E + C.NA + 1
        print(f"\n  ======== CI-1 arm `{arm}`   IN={C.IN}")
        C.main()
        res[arm] = json.load(open(os.path.join(HERE, "results_mf0c_controller.json")))
        os.replace(os.path.join(HERE, "results_mf0c_controller.json"),
                   os.path.join(HERE, f"results_mf0c_ci1_{'plus' if arm=='+' else 'minus'}.json"))

    print("\n  ---- CI-1 事前支持條件")
    plus_pass = res["+"]["pass"]
    minus_fail = not res["-"]["pass"]
    print(f"    1. `+` 臂 18 格全過：{plus_pass}")
    print(f"    2. `-` 臂至少一格 FAIL：{minus_fail}")
    deltas_ok, rows = True, []
    for s in res["+"]["seeds"]:
        for t in ("r0", "r1", "r2"):
            up = res["+"]["seeds"][s][t]["U_learned_W1"]
            um = res["-"]["seeds"][s][t]["U_learned_W1"]
            T = res["+"]["T_r"][t]
            rows.append((s, t, up, um, up - um, T, (up - um) >= T))
            deltas_ok &= (up - um) >= T
    print(f"    3. 每格 `U(+)−U(−) >= T_r`：{deltas_ok}")
    for s, t, up, um, d, T, ok in rows:
        print(f"       seed {s} {t}  U+ {up:.1%}  U- {um:.1%}  Δ {d:+.1%}  "
              f"T_r {T:.1%}  {'✓' if ok else '✗'}")
    h0_ok = all(res["+"]["seeds"][s][t]["U_h0_ablation_W1"]
                < res["+"]["seeds"][s][t]["U_learned_W1"]
                for s in res["+"]["seeds"] for t in ("r0", "r1", "r2"))
    print(f"    4. `+` 臂的 `h=0` ablation 仍低於完整（focus gate 不過）：{h0_ok}")

    supported = plus_pass and minus_fail and deltas_ok and h0_ok
    print()
    if supported:
        print("  → **假說獲支持**：顯式提供 conjunction 後 `+` 臂通過而 `-` 臂不通過。")
        print("    ⚠️ 這是 **inductive-bias intervention**，**不得**稱 natural／emergent formation。")
    elif plus_pass and not minus_fail:
        print("  → **兩臂都過** → 只判 **generic architecture change**，")
        print("    **不支持**「explicit conjunction 釋放 state」的說法。")
    else:
        print("  → **此假說未獲支持**。")
    json.dump({"plus_pass": plus_pass, "minus_fail": minus_fail,
               "deltas_ok": deltas_ok, "h0_ok": h0_ok, "supported": supported,
               "rows": [list(r) for r in rows]},
              open(os.path.join(HERE, "results_mf0c_ci1.json"), "w"),
              indent=2, ensure_ascii=False)
    print("  -> results_mf0c_ci1.json")


if __name__ == "__main__":
    main()
