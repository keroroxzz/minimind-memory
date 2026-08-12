"""橋接任務的 latent store ＋ **typed membership guard**。

§4.37–4.38 的結論在這裡做 targeted replication（§4.50 的 transfer matrix 要求）：

> **「這條記憶存不存在」是可判定的資料結構事實，不是學出來的相似度該猜的東西。**
> 在 S₅ 上，把它改成 `canonicalize(key) → store.contains(key)` 之後，
> 幻覺從 **8/300** 降到 **0/300**（單側 95% 上界 0.99%）。

所以本檔的 `contains` **不是**模型、**不是**相似度、**不做**閾值 —— 就是查表。
學出來的相似度只能作 **shadow metric**，**永遠不得覆寫 guard**。

### 契約（照 §4.34 的驗法，逐位元檢查）

- `address 可見 ⇔ committed content 可讀`
- commit 是原子的
- 讀到 dangling／key 不符 → **fail closed（abstain）**，不得猜

### 與 S₅ 版的差異

S₅ 的 key 是 5 token 的 span，這裡是 `(name, attr)`；
content 從 25 維 one-hot 換成 `z' = [addr(16), value, attr(3)]`。
**address 由 `φ` 決定性生成**，與 query 側同一個函數 —— 這正是 §4.55 成立的前提。
"""
import torch

import bridge_latent_schema as S
import bridge_renderer as B


class Store:
    """address → (key, content) 的持久映射。**沒有任何學習成分。**"""

    def __init__(self):
        self._d = {}          # canonical key -> (addr tensor, z' tensor)
        self._dangling = set()  # 索引還在、內容沒了（部分寫入／GC race 的代理）

    # ---- 寫入 --------------------------------------------------------
    @staticmethod
    def canonical(name, attr_idx):
        """唯一的 key 正規化入口。**任何一側都必須走這裡**，不得各自重新推導。

        CLAUDE.md 記過：符號比對必須共用同一個 canonical-span helper，
        自己在別處重算 tokenization 會產生看似合理的錯誤結論。
        """
        assert name in B.NAMES and 0 <= attr_idx < len(B.ATTRS)
        return (name, int(attr_idx))

    def commit(self, name, attr_idx, value):
        """原子寫入。回傳 key。"""
        k = self.canonical(name, attr_idx)
        z = S.fact_latent2(name, attr_idx, value)
        self._d[k] = (S.phi(name, attr_idx).clone(), z.clone().detach())
        return k

    # ---- 讀取 --------------------------------------------------------
    def contains(self, name, attr_idx):
        """**可判定的資料結構事實** —— 不是相似度、不是閾值。"""
        return self.canonical(name, attr_idx) in self._d

    def read(self, name, attr_idx):
        """回傳 `z'`；不存在則回 None（呼叫端必須 fail closed）。"""
        k = self.canonical(name, attr_idx)
        if k in self._dangling:
            return None           # **索引可見但內容不可讀** —— 契約被破壞的那一格
        e = self._d.get(k)
        return None if e is None else e[1]

    def dangle(self, name, attr_idx):
        """**故障注入專用**（`BR-G3c-D`）：打破 `address 可見 ⇔ content 可讀`。

        之後 `contains()` 仍為 True 而 `read()` 回 None，
        所以 `retrieve()` 必須走 `badread` —— **不得**退化成普通的 `absent`。
        正常路徑永遠不呼叫這個方法。
        """
        k = self.canonical(name, attr_idx)
        assert k in self._d, "只能對已 commit 的 entry 注入 dangling"
        self._dangling.add(k)
        return k

    def keys(self):
        return list(self._d)

    def __len__(self):
        return len(self._d)

    # ---- 契約自檢 ----------------------------------------------------
    def verify(self):
        """`address 可見 ⇔ content 可讀`，且 address↔content 綁定正確。"""
        bad = 0
        for (nm, ai), (addr, z) in self._d.items():
            if not torch.allclose(addr, S.phi(nm, ai)):
                bad += 1
            if not torch.allclose(z[:S.ADDR_DIM], S.phi(nm, ai)):
                bad += 1              # content 內嵌的 address 必須與索引一致
            if self.read(nm, ai) is None:
                bad += 1
        return bad


def shadow_similarity(store, name, attr_idx):
    """**shadow metric** —— 學不出來就用最近鄰餘弦模擬「相似度式檢索」。

    §4.38 的角色：它會漏掉的那些，正是 guard 擋下來的。
    **它永遠不得覆寫 guard**，只用來報「若沒有 guard 會怎樣」。
    """
    q = S.phi(name, attr_idx)
    best, bk = -2.0, None
    for k in store.keys():
        c = float(torch.dot(q, S.phi(*k)))
        if c > best:
            best, bk = c, k
    return bk, best


def retrieve(store, name, attr_idx):
    """**權威路徑**：canonicalize → contains → read → 斷言 → 交付或 fail closed。

    回傳 `(z, status)`；`status ∈ {ok, absent, badread, wrongkey}`。
    `absent` 一律在**注入之前**就 abstain —— 這是 halluc=0 的結構性理由。
    """
    if not store.contains(name, attr_idx):
        return None, "absent"
    z = store.read(name, attr_idx)
    if z is None:
        return None, "badread"
    if not torch.allclose(z[:S.ADDR_DIM], S.phi(name, attr_idx)):
        return None, "wrongkey"
    return z, "ok"


# ---------------------------------------------------------------- B0 描述定址
# 每個 entity 帶兩個獨立屬性（Codex [149]）：query 只給**屬性子集的 conjunction**、
# 不給 name。16 個 name 各分到一組互異的 (color, shape)，所以完整 conjunction
# 全域唯一；但 8 種顏色分給 16 個名字 → **每個單一屬性都有多個 distractor**，
# 這才是「交集解析」而不是把 `(name, attr)` 換個字串。
#
# resolver **是規則式的**：研究價值在資料／candidate-set／guard plumbing，
# **不宣稱學到語意**（Codex [149]）。
N_COLOR = N_SHAPE = 8


def _ent_table():
    """16 名字 → (color, shape)，**確定性構造**，同時滿足兩個硬條件：

    - **完整 conjunction 全域唯一**（16 個 pair 互異）
    - **每個單一屬性恰好有 2 個名字**（color 與 shape 皆是）→ 單屬性查詢必然歧義

    隨機分配做不到後者：實測會有 3 個顏色只分到 1 個名字，
    那些查詢就算「只給 color」也唯一命中，**B0-A 的歧義條件失效**。
    """
    t = {nm: (i // 2, (i // 2 + (i % 2) * (N_SHAPE // 2)) % N_SHAPE)
         for i, nm in enumerate(B.NAMES)}
    from collections import Counter
    assert len(set(t.values())) == len(B.NAMES), "conjunction 必須唯一"
    assert set(Counter(v[0] for v in t.values()).values()) == {2}, "每個 color 須恰 2 名"
    assert set(Counter(v[1] for v in t.values()).values()) == {2}, "每個 shape 須恰 2 名"
    return t


ENT = _ent_table()


def resolve(store, color=None, shape=None, attr_idx=0):
    """描述 → key。回傳 `(key_or_None, status)`。

    `status`：`ok`（唯一命中）／`ambiguous`（多候選 → **hard-abstain，不得任意挑**）／
    `absent`（無候選，或候選不在 store 裡）。

    ⚠️ 這是**可判定的約束解析**，不是 semantic retrieval ——
    措辭限定為 `description-to-unique-key resolution`（Codex [149]）。
    """
    cand = [nm for nm in B.NAMES
            if (color is None or ENT[nm][0] == color)
            and (shape is None or ENT[nm][1] == shape)
            and store.contains(nm, attr_idx)]
    if len(cand) > 1:
        return None, "ambiguous"
    if not cand:
        return None, "absent"
    return (cand[0], attr_idx), "ok"


# ------------------------------------------------------------ B0-N 噪聲描述
# **明確的 Hamming error model，不是語意相似度**（Codex [159] 回覆鎖定規格）：
#
#     d(q, e) = 1[color 不同] + 1[shape 不同]      ∈ {0, 1, 2}
#
# 值域與 `ENT` 兩欄完全沿用 B0，**不新增任何學習成分、不設閾值**。
# 措辭限定為 `noisy symbolic descriptor resolution`；
# **不得**叫作 semantic／open-set retrieval，PASS 也不得升格。


def hamming(q_color, q_shape, name):
    c, s = ENT[name]
    return int(c != q_color) + int(s != q_shape)


def resolve_noisy(store, q_color, q_shape, attr_idx):
    """噪聲描述 → key。**規則開跑前鎖死，不得因結果調整。**

    只在 `d1 == 1` 且 `d2 - d1 >= 1` 時回傳唯一最近鄰：

    - `d1 == 1 and d2 == 1` → `near_tie`  → **hard-abstain**
    - `d1 >= 2`             → `no_match`  → **hard-abstain**
    - `d1 == 0`             → `exact`     → 交給 B0-U 的精確路徑，不歸 B0-N 管

    回傳 `(key_or_None, status)`。
    """
    cand = sorted(((hamming(q_color, q_shape, nm), nm) for nm in B.NAMES
                   if store.contains(nm, attr_idx)))
    if not cand:
        return None, "no_match"
    d1 = cand[0][0]
    if d1 == 0:
        return None, "exact"
    if d1 >= 2:
        return None, "no_match"
    d2 = cand[1][0] if len(cand) > 1 else 99
    if d2 - d1 < 1:
        return None, "near_tie"
    return (cand[0][1], attr_idx), "ok"
