"""C 層記憶模組 —— G1 vertical slice。

規格見 `design_c_layer.md`。這一版只實作 G1 需要的最小集合：

  oracle-selected value → position-neutral latent → learned delivery → composition

**刻意不做**：semantic/ANN retrieval、utility/eviction、learned consolidate/merge、
learned write policy、獨立可訓練的 WM 層。理由見規格 §0 ——
三段同時可學時，失敗無法定位。

唯一可學的是 `Delivery`。store 凍結、selection 是 oracle。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol

import torch
import torch.nn as nn

PERM_N = 5
LATENT_DIM = PERM_N * PERM_N          # 25：5x5 permutation matrix 攤平
                                      # 資訊完備、canonical、無 encoder（規格 §1）


# --------------------------------------------------------------------------- 型別

@dataclass
class MemoryEntry:
    """一條記憶。`latent` 是 position-neutral 的內容表示。"""
    address: str                       # G1 用 exact key（'f0' 等）
    latent: torch.Tensor               # (LATENT_DIM,)
    metadata: dict = field(default_factory=dict)
    version: int = 0


@dataclass
class ActiveWorkspace:
    """共享暫態，**不是層**（研究規劃 §6）。

    具體就是 loop 之間攜帶的 hidden state，加上 recent KV 與已交付的 carrier。
    沒有「訓練 WM 層」這回事 —— §4.20/§8.1 沒有可靠證據要求它。
    """
    hidden: torch.Tensor | None = None          # (B, T, H)
    kv: list | None = None                      # per-layer past_key_values
    carriers: torch.Tensor | None = None        # (B, M, H) 已交付的 memory carrier
    carrier_mask: torch.Tensor | None = None    # (B, M) bool


class LatentStore:
    """只負責持久化與物理 commit，**沒有任何策略**（規格 §1）。

    模型不吐 CRUD —— 這是 backend 介面。
    `commit` 一定 detach：跨 episode 不回傳梯度（規格 §3）。
    """

    def __init__(self):
        self._entries: dict[str, MemoryEntry] = {}

    def commit(self, entries) -> None:
        for e in entries:
            # detach + clone：切斷 graph，且不與呼叫端共用儲存
            self._entries[e.address] = MemoryEntry(
                address=e.address,
                latent=e.latent.detach().clone(),
                metadata=dict(e.metadata),
                version=self._entries.get(e.address, e).version + 1
                if e.address in self._entries else e.version,
            )

    def read(self, addresses):
        """snapshot 語意：回傳當下的值，查不到回 None（不是靜默回垃圾）。"""
        return [self._entries.get(a) for a in addresses]

    def __len__(self):
        return len(self._entries)

    @property
    def capacity(self) -> int:
        return len(self._entries)


class OracleMemoryInterface:
    """G1 的 selection 是 oracle —— 固定、無梯度（規格 §3）。

    `select_many` 而非單數 `select`：單數 API 表達不了 k 步的 composition chain。
    oracle 把 chain 解成**有序的 k 條目 list（元素可重複）**。
    """

    def __init__(self, store: LatentStore):
        self.store = store

    @torch.no_grad()
    def select_many(self, chain):
        entries = self.store.read(chain)
        support = torch.tensor([0.0 if e is None else 1.0 for e in entries])
        return entries, support


# --------------------------------------------------------------------------- Delivery

class Delivery(Protocol):
    def apply(self, ws: ActiveWorkspace) -> ActiveWorkspace: ...


def _stack_latents(entries, device, dtype):
    """把 k 條目堆成 (k, LATENT_DIM)。缺項補零，並回傳 mask。"""
    lat = torch.stack([
        (e.latent if e is not None else torch.zeros(LATENT_DIM)).to(device=device, dtype=dtype)
        for e in entries
    ])
    mask = torch.tensor([e is not None for e in entries], device=device)
    return lat, mask


class LatentSlotAdapter(nn.Module):
    """latent (25,) → carrier (H,)，寫進 workspace.carriers 由 core 以 attention 讀取。

    這是 **G1 唯一可學的組件**。
    """

    def __init__(self, hidden_size: int, latent_dim: int = LATENT_DIM, width: int = 256):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(latent_dim, width), nn.GELU(), nn.Linear(width, hidden_size),
        )

    def forward(self, latents):                      # (B, k, 25) -> (B, k, H)
        return self.net(latents)


class SyntheticKVAdapter(nn.Module):
    """latent (25,) → 每層的合成 K/V，直接插進 past_key_values。

    **不套 RoPE** —— latent 依定義是 position-neutral 的，
    合成條目不對應任何序列位置。這是設計選擇，不是實測結論。
    """

    def __init__(self, n_layers: int, n_kv_heads: int, head_dim: int,
                 latent_dim: int = LATENT_DIM, width: int = 256):
        super().__init__()
        self.n_layers, self.n_kv_heads, self.head_dim = n_layers, n_kv_heads, head_dim
        out = n_layers * 2 * n_kv_heads * head_dim
        self.net = nn.Sequential(
            nn.Linear(latent_dim, width), nn.GELU(), nn.Linear(width, out),
        )
        # 最後一層縮小初始化：合成條目起始時對 attention 的擾動小，
        # 避免一開始就淹沒真實 KV
        nn.init.normal_(self.net[-1].weight, std=0.02 / math.sqrt(width))
        nn.init.zeros_(self.net[-1].bias)

    def forward(self, latents):                      # (B, k, 25)
        B, k, _ = latents.shape
        o = self.net(latents)
        o = o.view(B, k, self.n_layers, 2, self.n_kv_heads, self.head_dim)
        # -> per-layer (K, V)，各為 (B, k, n_kv_heads, head_dim)
        return [(o[:, :, l, 0], o[:, :, l, 1]) for l in range(self.n_layers)]


# --------------------------------------------------------------------------- 三種交付

class InlineTokensDelivery:
    """把值展開成 token 接在使用處。

    ⚠️ 這是 **positive control，不是 latent delivery**（規格 §5）——
    不能拿它來論證 latent 路徑成立。
    """
    is_latent_path = False


class LatentSlotsDelivery:
    is_latent_path = True

    def __init__(self, adapter: LatentSlotAdapter):
        self.adapter = adapter

    def apply(self, ws: ActiveWorkspace, latents, mask) -> ActiveWorkspace:
        carriers = self.adapter(latents)
        return ActiveWorkspace(hidden=ws.hidden, kv=ws.kv,
                               carriers=carriers, carrier_mask=mask)


class SyntheticKVDelivery:
    is_latent_path = True

    def __init__(self, adapter: SyntheticKVAdapter):
        self.adapter = adapter

    def apply(self, ws: ActiveWorkspace, latents, mask) -> ActiveWorkspace:
        synth = self.adapter(latents)
        if ws.kv is None:
            kv = synth
        else:
            kv = [(torch.cat([sk, k], dim=1), torch.cat([sv, v], dim=1))
                  for (sk, sv), (k, v) in zip(synth, ws.kv)]
        return ActiveWorkspace(hidden=ws.hidden, kv=kv,
                               carriers=ws.carriers, carrier_mask=ws.carrier_mask)


# --------------------------------------------------------------------------- 工具

def perm_to_latent(perm) -> torch.Tensor:
    """S₅ 置換 → 5x5 permutation matrix → 攤平 (25,)。資訊完備、可逆。"""
    m = torch.zeros(PERM_N, PERM_N)
    for i, v in enumerate(perm):
        m[i, v] = 1.0
    return m.reshape(-1)


def latent_to_perm(latent) -> list:
    """反解，用來驗證 latent 無損。"""
    return latent.reshape(PERM_N, PERM_N).argmax(dim=-1).tolist()
