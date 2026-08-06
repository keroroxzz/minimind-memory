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
        e = self._d.get(self.canonical(name, attr_idx))
        return None if e is None else e[1]

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
