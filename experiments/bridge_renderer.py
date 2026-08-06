"""橋接任務的資料層 —— 受控語法的自然語言表面 + **連續**內容。

§4.50 定案：S₅ 已到邊界，因為五個結構軸把開放語意記憶壓成 closed algebra。
這個任務逐一補上：

| S₅ 的性質 | 這裡 |
|---|---|
| 內容離散（120 選 1，無序）| **連續純量**（0..99 有序、可分級）|
| 最小無損碼、零冗餘 | latent 是**實數**，失真是連續的、有意義的 |
| 無「相關但不相同」 | 34 與 35 **相近**；比較與排序都有定義 |
| 單一群運算 | **非交換且非結合的狀態更新**（見下）|

**仍然保留的（這是可證偽性的來源）**：答案由建構決定、可精確驗證、
天花板可量、可做 paired logit 分解。

### ⚠️ 合成必須非交換**且**非結合 —— 第一版在這裡犯了已記錄過的錯

第一版用「鏈式比較」：`a 與 b 中較大者 與 c 中較大者` **就是 `max(a,b,c)`**。
`max` **可交換也可結合**，所以那個「鏈」不是鏈 —— 模型可以平行掃描選最大值，
`j` **根本不是深度軸**。

這與 CLAUDE.md 記錄、S₅ 任務第二版被撤回的原因是**同一個形狀**：
純加減塌成 `v0 + Σ±r`（TC⁰），因此看不到深度天花板。
`sum`、`max`、`min`、`xor` 全部有這個問題。

現行的更新是 **`x ← |x − v|`**：

- **非交換**：`|(|x−a|)−b| ≠ |(|x−b|)−a|`
- **非結合**：絕對值是非線性的，不能重新括號
- **連續**：值域仍是 0..99 的有序量，鄰近仍有意義
- **可精確驗證**：整數運算，答案由建構決定

所以 `j` 步真的需要 `j` 次順序相依的計算。

### 兩種 render 必須並存（Codex 鎖死的一級變因）

同一語法、同一答案，同時有 `L0`（顯式值）與 `carrier`（placeholder）兩種：

    L0       | anna a 是 3 4 | ben a 是 7 1 | 問 anna a 大於 ben a 嗎 ?
    carrier  | anna a 是 . . | ben a 是 . . | 問 anna a 大於 ben a 嗎 ?

**train/val/test 都要涵蓋兩種**，否則會重演 G4b 那個「core 不認得格式、
oracle 天花板掉到 0.0%」的錯 —— 那個錯與記憶完全無關。

### 載體位置

第一版仍是**模板／oracle binding**，但**隨機化合法 slot 與位置**（實體數、
屬性數、問哪一個都隨機），這是為了隔離「消費／融合」而不是重演 writer-location。
**「模型自己決定放哪」另立後續軸。**

### 數字的切法

CLAUDE.md 記過：這個 6400 vocab 對兩位數的切法不一致（`'60'→[3873]` 但
`'97'→[60,58]`）。所以值一律 render 成**空格分隔的個位數**，
每個值恰好 2 個 token，`L0` 與 `carrier` 才逐 token 對齊。
"""
import hashlib
import json
import random

NAMES = ["anna", "ben", "cara", "dan", "elsa", "finn", "gina", "hugo",
         "iris", "jack", "kira", "leo", "mia", "noah", "ova", "pete"]
ATTRS = ["a", "b", "c"]
VMIN, VMAX = 10, 99            # 兩位數 → 恰好 2 個 token（空格分隔）
PLACE = ". ."                  # 與一個值同樣是 2 個 token
LATENT_DIM = 4                 # [值/100, 屬性 one-hot(3)]；**值是連續純量**


def val_str(v):
    """一律兩位、空格分隔 —— 這個 vocab 對兩位數的切法不一致（CLAUDE.md）。

    `|x-v|` 的結果可能是個位數，補零維持恰好 2 個 token，
    否則 L0 與 carrier 的 token 對齊會破掉。
    """
    v = max(0, min(99, int(v)))
    return f"{v//10} {v%10}"


def fact_latent(v, attr_idx):
    """**連續** latent：第 0 維是 v/100 的實數，其餘是屬性 one-hot。

    與 S₅ 的 25 維 one-hot 的關鍵差異 —— 這裡 34 與 35 的 latent 相距 0.01，
    是**有意義的鄰近**；one-hot 下它們正交。
    """
    z = [0.0] * LATENT_DIM
    z[0] = v / 100.0
    z[1 + attr_idx] = 1.0
    return z


def latent_to_val(z):
    """反解出值（四捨五入到整數）。連續 schema 下這是**近似**解碼，不是精確碼字。"""
    return int(round(z[0] * 100))


class Episode:
    """一題的表示無關核心。兩種 render 共用同一實例。"""

    def __init__(self, facts, query, answer, ask_idx, j):
        self.facts = facts          # [(name, attr_idx, value)]，有序
        self.query = query          # 人類可讀的問句片段
        self.answer = answer        # 字串，逐 token 可驗
        self.ask_idx = ask_idx      # 這題真正用到哪幾條 fact（有序）
        self.j = j                  # 0 = 純讀出；>=1 = 融合步數

    @property
    def eid(self):
        core = json.dumps({"f": self.facts, "q": self.query,
                           "a": self.answer, "j": self.j}, sort_keys=True)
        return hashlib.sha256(core.encode()).hexdigest()[:16]


def _facts_block(facts, carrier_mask):
    """carrier_mask[i] 為 True 時，第 i 條 fact 的值換成 placeholder。"""
    out = []
    for i, (nm, ai, v) in enumerate(facts):
        val = PLACE if carrier_mask[i] else val_str(v)
        out.append(f"| {nm} {ATTRS[ai]} 是 {val}")
    return " ".join(out)


def render(ep, carrier_mask=None):
    """回傳 (prompt, answer)。`carrier_mask=None` 即 L0（全部顯式）。"""
    if carrier_mask is None:
        carrier_mask = [False] * len(ep.facts)
    return f"{_facts_block(ep.facts, carrier_mask)} 問 {ep.query} ?", ep.answer


def value_positions(tok, ep, carrier_mask):
    """placeholder 那些值的 token 位置 —— 用「換成數字再對位相減」求得。

    ⚠️ 與 S₅ 同一個手法，但這裡的理由更重要：`L0` 與 `carrier` 必須逐 token
       對齊，否則交付會落在錯的位置，而那會被誤判成融合失敗。
    """
    import torch
    a = tok(tok.bos_token + render(ep, carrier_mask)[0],
            add_special_tokens=False).input_ids
    b = tok(tok.bos_token + render(ep, None)[0], add_special_tokens=False).input_ids
    assert len(a) == len(b), "L0 與 carrier 的 token 長度必須相同"
    pos = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    want = sum(carrier_mask) * 2
    assert len(pos) == want, f"carrier 位置 {len(pos)} != {want}"
    return torch.tensor(a), torch.tensor(pos)


def make_episode(rng, j, n_fact=None, same_name=False):
    """`j=0` 純讀出；`j>=1` 鏈式比較。答案一律由建構決定。

    `same_name` 控制「同一個實體是否可以有多條記憶」：

    - `False`（**預設，與歷史行為逐字相同**）：`rng.sample(NAMES, n_fact)`，
      同一題內名字必定互異 —— §4.55 之前所有實驗都是這個分布。
    - `True`：**保證至少一組同名不同屬性**。
    - `None`：從 `(name, attr)` 的全集自然抽樣，同名自然發生。

    ⚠️ 這個旗標的存在本身就是一個結論：舊分布下 core 從沒見過
       「同一個實體的兩條記憶」，所以它可以只靠**名字**匹配 address 而不看屬性。
       實測同名 distractor 時模型**輸出兩值的平均**（90.5% 落在兩值之間，
       |pred − 平均| 中位數 2.5）—— 那是 name-only 匹配造成的 value 混合。
       `(name, attr)` 一律唯一；**同一 exact key 兩個不同值屬於 conflict 軸，不在此**。
    """
    n_fact = n_fact or rng.randint(2, 4)
    if same_name is False:
        names = rng.sample(NAMES, n_fact)
        pairs = [(nm, rng.randrange(len(ATTRS))) for nm in names]
    else:
        allp = [(nm, ai) for nm in NAMES for ai in range(len(ATTRS))]
        if same_name is True and n_fact >= 2:
            nm = NAMES[rng.randrange(len(NAMES))]
            ats = rng.sample(range(len(ATTRS)), 2)
            pairs = [(nm, ats[0]), (nm, ats[1])]
            rest = [p for p in allp if p not in pairs]
            pairs += rng.sample(rest, n_fact - 2)
            rng.shuffle(pairs)
        else:
            pairs = rng.sample(allp, n_fact)
    facts = [(nm, ai, rng.randint(VMIN, VMAX)) for nm, ai in pairs]
    # 值互異：避免平手，也避免 |x-v| 中途歸零讓後續步驟退化
    while len({v for _, _, v in facts}) < n_fact:
        facts = [(nm, ai, rng.randint(VMIN, VMAX)) for nm, ai, _ in facts]
    assert len({(nm, ai) for nm, ai, _ in facts}) == n_fact, "(name, attr) 必須唯一"

    if j == 0:
        i = rng.randrange(n_fact)
        nm, ai, v = facts[i]
        return Episode(facts, f"{nm} {ATTRS[ai]} 是 多少", val_str(v), [i], 0)

    # j 步 `x ← |x − v|`：**非交換且非結合**，所以順序真的重要。
    # 起點是第一條 fact 的值，之後每一步差一次。
    # ⚠️ 相鄰不得重複：`x 差 x` 恆為 0，會產生大量退化題並讓 j 失去意義。
    #    但允許非相鄰重複（同一個 entity 可以在鏈中出現多次）。
    idx = [rng.randrange(n_fact)]
    while len(idx) < j + 1:
        c = rng.randrange(n_fact)
        if c != idx[-1]:
            idx.append(c)
    cur = facts[idx[0]][2]
    q = f"{facts[idx[0]][0]} {ATTRS[facts[idx[0]][1]]}"
    for nxt in idx[1:]:
        q = f"{q} 差 {facts[nxt][0]} {ATTRS[facts[nxt][1]]}"
        cur = abs(cur - facts[nxt][2])
    return Episode(facts, f"{q} 是 多少", val_str(cur), sorted(set(idx)), j)


def episode_latents(ep, carrier_mask):
    """被 placeholder 掉的那些 fact 的**連續** latent，順序與 carrier 位置一致。"""
    return [fact_latent(v, ai) for (nm, ai, v), m
            in zip(ep.facts, carrier_mask) if m]


def random_mask(rng, ep, p=0.5, force_used=False):
    """隨機化哪些 slot 變成 carrier（Codex：隨機化合法 slot 與位置）。

    `force_used=True` 時保證這題真正用到的 fact 都走 carrier ——
    那才是「記憶必須被消費」的條件。
    """
    m = [rng.random() < p for _ in ep.facts]
    if force_used:
        for i in ep.ask_idx:
            m[i] = True
    return m
