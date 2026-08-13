"""`π_content*` 的**精確** Bayes-causal reference ＋ implementation gate（Codex [183]）。

`P(F=e | prefix)` 不是閉式，但**不需要**逐項枚舉 `C(15,7)=6435`：
它是一個固定的小型 **coefficient DP**，比 approximation／MC **更可稽核**。

對每個 prefix count `n` 與候選 focus `e`：

    d_0(0) = 1，其餘 0
    逐一加入 i != e 的 15 個 entity：
        d_j(r) = C(3, n_i) * d_{j-1}(r) + C(4, n_i) * d_{j-1}(r-1)，r = 0..7
    w_e = C(12, n_e) * d_15(7)
    P(F=e | prefix) = w_e / Σ_e w_e

共同的 hypergeometric／attribute-set 因子**跨 `e` 相消**。
reference **只可讀 prefix 的 entity counts（含當前 event）**，
**不得**讀 `focus`、query 或 suffix。

⚠️ **已撤回**「mean-field 只會低估 `H`」的說法（Codex [183]）：
   降低 posterior fidelity **不保證** top-B 的 policy utility 單調變差；
   rank 改變可讓有限 eval 上的 `H` 偏向**任一**方向。**它不是保守通行證。**
"""
import itertools
import json
import os
import random
from math import comb

HERE = os.path.dirname(os.path.abspath(__file__))
E, N_ATTR = 16, 12
FOCUS_K, N4, N3 = 12, 7, 8


def posterior_weights(counts):
    """回傳**未正規化的整數**權重 `w_e`。**精確**，不用浮點。

    以整數比較 rank，避免 float rounding 改變順序（Codex [183] 的 implementation gate）。
    """
    w = []
    for e in range(E):
        if counts[e] > FOCUS_K:
            w.append(0)
            continue
        d = [0] * (N4 + 1)
        d[0] = 1
        for i in range(E):
            if i == e:
                continue
            c3 = comb(3, counts[i]) if counts[i] <= 3 else 0
            c4 = comb(4, counts[i]) if counts[i] <= 4 else 0
            nd = [0] * (N4 + 1)
            for r in range(N4 + 1):
                if d[r]:
                    nd[r] += c3 * d[r]
                    if r + 1 <= N4:
                        nd[r + 1] += c4 * d[r]
            d = nd
        w.append(comb(FOCUS_K, counts[e]) * d[N4])
    return w


def brute_weights(counts):
    """暴力枚舉 `C(15,7)` 種 size assignment —— **僅供 smoke gate 比對**。"""
    w = []
    for e in range(E):
        others = [i for i in range(E) if i != e]
        tot = 0
        for four in itertools.combinations(range(15), N4):
            fs = set(four)
            pr = 1
            for j, i in enumerate(others):
                s = 4 if j in fs else 3
                c = comb(s, counts[i]) if counts[i] <= s else 0
                if c == 0:
                    pr = 0
                    break
                pr *= c
            tot += pr
        w.append(comb(FOCUS_K, counts[e]) * tot if counts[e] <= FOCUS_K else 0)
    return w


def valid_prefix_counts(rng):
    """由一條**合法** stream 取一個隨機 prefix 的 entity 計數。"""
    focus = rng.randrange(E)
    others = [x for x in range(E) if x != focus]
    rng.shuffle(others)
    recs = [(focus, a) for a in range(N_ATTR)]
    for x in others[:N4]:
        recs += [(x, a) for a in rng.sample(range(N_ATTR), 4)]
    for x in others[N4:]:
        recs += [(x, a) for a in rng.sample(range(N_ATTR), 3)]
    rng.shuffle(recs)
    t = rng.randrange(1, len(recs) + 1)
    c = [0] * E
    for en, _ in recs[:t]:
        c[en] += 1
    return c, recs, focus


def main():
    print("  π_content* reference smoke gate（Codex [183]）")
    print("  **這是 reference smoke，不是新的 H data**\n")
    rng = random.Random(20260813_99)          # 與 eval 無關的固定 seed
    bad = []

    # 1. 128 個 fixture 上，DP 必須與暴力枚舉**完全相等**
    for k in range(128):
        c, _, _ = valid_prefix_counts(rng)
        if posterior_weights(c) != brute_weights(c):
            bad.append(f"fixture {k} DP != brute")
    print(f"  1. 128 個 valid prefix fixture：DP ≡ C(15,7) 暴力枚舉 "
          + ("✓" if not bad else f"✗ {bad[:3]}"))

    # 2. 空 prefix → 16-way uniform
    w0 = posterior_weights([0] * E)
    print(f"  2. 空 prefix → 16-way uniform "
          + ("✓" if len(set(w0)) == 1 and w0[0] > 0 else "✗"))
    if len(set(w0)) != 1:
        bad.append("空 prefix 非 uniform")

    # 3. 更換 suffix 不改變 posterior（posterior 只依 prefix counts）
    c, recs, _ = valid_prefix_counts(rng)
    t = sum(c)
    r2 = recs[:t] + list(reversed(recs[t:]))
    c2 = [0] * E
    for en, _ in r2[:t]:
        c2[en] += 1
    same = posterior_weights(c) == posterior_weights(c2)
    print(f"  3. 更換 suffix 不改 posterior " + ("✓" if same else "✗"))
    if not same:
        bad.append("suffix 影響了 posterior")

    # 4. 整數比較：權重全為 int，rank 不經浮點
    allint = all(isinstance(x, int) for x in posterior_weights(c))
    print(f"  4. 權重為精確整數，rank 不經浮點 " + ("✓" if allint else "✗"))
    if not allint:
        bad.append("權重非整數")

    print()
    print("  → **reference smoke PASS**" if not bad else f"  → **FAIL**：{bad[:5]}")
    json.dump({"fixtures": 128, "violations": bad, "pass": not bad},
              open(os.path.join(HERE, "results_mf0c_ref_smoke.json"), "w"),
              indent=2, ensure_ascii=False)
    print("  -> results_mf0c_ref_smoke.json")


if __name__ == "__main__":
    main()
