"""橋接任務的資料層 —— 受控語法的自然語言表面 + **連續**內容。

§4.50 定案：S₅ 已到邊界，因為五個結構軸把開放語意記憶壓成 closed algebra。
這個任務逐一補上：

| S₅ 的性質 | 這裡 |
|---|---|
| 內容離散（120 選 1，無序）| **連續純量**（0..99 有序、可分級）|
| 最小無損碼、零冗餘 | latent 是**實數**，失真是連續的、有意義的 |
| 無「相關但不相同」 | 34 與 35 **相近**；比較與排序都有定義 |
| 單一群運算 | **比較 + 鏈式比較**（非交換、非代數封閉）|

**仍然保留的（這是可證偽性的來源）**：答案由建構決定、可精確驗證、
天花板可量、可做 paired logit 分解。

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


def make_episode(rng, j, n_fact=None):
    """`j=0` 純讀出；`j>=1` 鏈式比較。答案一律由建構決定。"""
    n_fact = n_fact or rng.randint(2, 4)
    names = rng.sample(NAMES, n_fact)
    facts = [(nm, rng.randrange(len(ATTRS)), rng.randint(VMIN, VMAX))
             for nm in names]
    # 值互異，避免平手讓答案不唯一
    while len({v for _, _, v in facts}) < n_fact:
        facts = [(nm, ai, rng.randint(VMIN, VMAX)) for nm, ai, _ in facts]

    if j == 0:
        i = rng.randrange(n_fact)
        nm, ai, v = facts[i]
        return Episode(facts, f"{nm} {ATTRS[ai]} 是 多少", val_str(v), [i], 0)

    # j 步鏈式比較：每一步把「目前較大的那個」與下一條比
    idx = rng.sample(range(n_fact), min(j + 1, n_fact))
    cur = idx[0]
    q = f"{facts[cur][0]} {ATTRS[facts[cur][1]]}"
    for nxt in idx[1:]:
        q = f"{q} 與 {facts[nxt][0]} {ATTRS[facts[nxt][1]]} 中 較 大 者"
        cur = cur if facts[cur][2] > facts[nxt][2] else nxt
    return Episode(facts, f"{q} 是 多少", val_str(facts[cur][2]), idx, len(idx) - 1)


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
