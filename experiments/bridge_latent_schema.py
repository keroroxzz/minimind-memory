"""latent-only 的 schema 與 render —— **文字裡不再有事實,也沒有 placeholder**。

`BRIDGE_PREREG_latent.md` 是規格,本檔只實作。

### 舊 schema 為什麼作廢

`z = [值/100, one-hot(attr,3)]` 裡**沒有 entity 欄位**:

    「dan  b = 47」 -> [0.47, 0, 1, 0]
    「anna b = 47」 -> [0.47, 0, 1, 0]     ← 完全相同

屬性只有 3 種而一題最多 4 條 fact,鴿籠原理保證必有兩條屬性相同。
**資訊論上不可能從這個碼恢復身份**,所以多條同時注入只能亂猜(`1/n`)。
這不是訓練能補的(Codex [138])。

### 新 schema

    z' = [ addr(16) , value/100 (1) , attr one-hot(3) ]      共 20 維
           └─ φ(name, attr)：固定、確定性、未訓練

`φ` 對 memory 側與 query 側是同一個函數。**query 側不注入任何東西** ——
問題的 entity 就寫在文字裡,core 必須學會把文字 entity 與 carrier 的 addr 對上。

### render

    L0     : | anna a 是 4 7 | ben b 是 8 2 問 anna a 是 多少 ?
    latent : 問 anna a 是 多少 ?          ← 文字裡**完全沒有事實**
             + 記憶以 prepended carrier embedding 進入(不經 tokenizer)

⚠️ carrier **確實佔用序列位置**(RoPE 位移),只是**不佔文字 token**。不得宣稱省 context。
"""
import torch

import bridge_renderer as B

ADDR_DIM = 16                    # φ 的輸出維度,鎖死
LAT_DIM = ADDR_DIM + 1 + 3       # addr + value + attr one-hot = 20
PHI_SEED = 20260806              # φ 的 seed,鎖死


def _phi_table():
    """`φ(name, attr)` 的查表版 —— 固定、確定性、**永不訓練**。

    16 名字 × 3 屬性 = 48 個 descriptor,用固定 seed 的高斯碼(單位化)。
    48 個 16 維隨機碼近似正交,足以區分;不用 one-hot 是因為要能擴充到未見 descriptor。
    """
    g = torch.Generator().manual_seed(PHI_SEED)
    t = torch.randn(len(B.NAMES), len(B.ATTRS), ADDR_DIM, generator=g)
    return t / t.norm(dim=-1, keepdim=True)


PHI = _phi_table()


def phi(name, attr_idx):
    """memory 側與 query 側共用的同一個確定性函數。"""
    return PHI[B.NAMES.index(name), attr_idx]


def fact_latent2(name, attr_idx, v):
    """`z' = [addr, value/100, attr one-hot]`。**含身份**,這是與舊 schema 的關鍵差異。"""
    z = torch.zeros(LAT_DIM)
    z[:ADDR_DIM] = phi(name, attr_idx)
    z[ADDR_DIM] = v / 100.0
    z[ADDR_DIM + 1 + attr_idx] = 1.0
    return z


def episode_latents2(ep, order=None):
    """回傳 (latents, order)。`order` 是 carrier 的排列 —— **必須隨機化**。

    否則模型可以用「第 k 個 carrier」取代 address,那就測不到身份比對。
    """
    n = len(ep.facts)
    order = list(range(n)) if order is None else order
    lats = torch.stack([fact_latent2(*ep.facts[i]) for i in order])
    return lats, order


def render_latent(ep):
    """latent render 的文字 —— **只有問句,沒有事實區、沒有 placeholder**。"""
    return f"問 {ep.query} ?", ep.answer


def render_l0(ep):
    """L0 天花板 —— 值全部寫在文字裡（沿用舊 renderer）。"""
    return B.render(ep, None)


class CarrierProj(torch.nn.Module):
    """`z' → hidden` 的**固定隨機投影,不進 optimizer**。

    與 `bridge_train_core.py` 的 oracle inline 同一個原則：
    若這層可訓練,core 會學會某個特定 adapter 的輸出,
    **後續的 delivery / retriever 實驗就失去意義**。
    """

    def __init__(self, hidden, scale=1.0, seed=PHI_SEED + 1):
        super().__init__()
        g = torch.Generator().manual_seed(seed)
        w = torch.randn(LAT_DIM, hidden, generator=g) / LAT_DIM ** 0.5
        self.register_buffer("w", w)
        self.register_buffer("scale", torch.tensor(float(scale)))

    def forward(self, z):
        return (z @ self.w) * self.scale


def fit_scale(proj, embed_weight, sample=None):
    """把 carrier embedding 的範數對齊 token embedding 的平均範數。

    不對齊的話 carrier 在數值上與 token 差一個量級,core 要花很多步只為了適應尺度。
    """
    with torch.no_grad():
        tgt = embed_weight.norm(dim=-1).mean()
        z = sample if sample is not None else torch.randn(256, LAT_DIM)
        cur = (z.to(proj.w.device) @ proj.w).norm(dim=-1).mean()
        proj.scale.fill_(float(tgt / cur))
    return float(proj.scale)


def latent_value(z):
    """從 `z'` 取回值（四捨五入到整數）。與 `fact_latent2` 的第 ADDR_DIM 維對應。"""
    return int(round(float(z[ADDR_DIM]) * 100))
