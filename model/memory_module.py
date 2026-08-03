"""C 層記憶模組 —— G1 vertical slice。

規格見 `design_c_layer.md`。這一版只實作 G1 需要的最小集合：

  oracle-selected value → position-neutral latent → learned delivery → composition

**刻意不做**：semantic/ANN retrieval、utility/eviction、learned consolidate/merge、
learned write policy、獨立可訓練的 WM 層。理由見規格 §0 ——
三段同時可學時，失敗無法定位。

唯一可學的是 `Delivery`。store 凍結、selection 是 oracle。
"""
from __future__ import annotations

import copy
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
                metadata=copy.deepcopy(e.metadata),
                version=self._entries.get(e.address, e).version + 1
                if e.address in self._entries else e.version,
            )

    def read(self, addresses):
        """**真正的 snapshot**：回傳 detach-clone 的副本，查不到回 None。

        先前回傳內部 entry 的引用，呼叫端就地改 latent/metadata 會破壞 store ——
        那不是 snapshot（Codex 指出）。
        """
        out = []
        for a in addresses:
            e = self._entries.get(a)
            out.append(None if e is None else MemoryEntry(
                address=e.address, latent=e.latent.detach().clone(),
                metadata=copy.deepcopy(e.metadata), version=e.version))
        return out

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
    is_latent_path: bool
    def apply(self, ws: ActiveWorkspace, latents: torch.Tensor,
              mask: torch.Tensor) -> ActiveWorkspace: ...


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
    """latent (25,) → 每層的合成 K/V，插進 past_key_values。

    **K 必須套 RoPE（Codex 修正）。** `position-neutral` 描述的是 *store 裡的 latent*，
    不代表 delivery 時的 key 不使用位置 —— native cache 存的是 **已 RoPE** 的 K。
    裸插無 RoPE 的 K 等於冒充 native cache 卻活在另一個空間。
    做法：給 memory slot 固定的 **virtual prefix positions 0..k-1**，
    用與該層相同的旋轉套上去；真實 token 的位置由 `past_len` 自然後移。

    （若日後真要 positionless，需另開 memory-attention/cross-attn 路徑。）

    **初始化用標準 Linear init（Codex 修正）**：先前縮小最後一層是為了
    「起始不淹沒真實 KV」，但那會讓回傳到前層的梯度也變小，
    而且 K/V 進 softmax 後即使接近 0 仍佔分母，**本來就不是 no-op**。
    尺度對齊改用 `scale`（由 L0 實測的 native K/V RMS 設定），
    而 small-batch overfit 是防止假失敗的閘。
    """

    def __init__(self, n_layers: int, n_kv_heads: int, head_dim: int,
                 latent_dim: int = LATENT_DIM, width: int = 256,
                 num_loops: int = 1, rope_base: float = 1e6,
                 scale_k=None, scale_v=None):
        super().__init__()
        self.n_layers, self.n_kv_heads, self.head_dim = n_layers, n_kv_heads, head_dim
        self.num_loops = num_loops
        self.n_slots = n_layers * num_loops       # cache 的實際注入位置數
        # **逐注入位置**對準 native RMS。先前只有 n_layers 個 scale，
        # 但 loop2 有 16 個位置，而實測顯示**同層跨 loop 也不同**
        # （L1 的 V：loop0 0.42 → loop1 0.87，超過 2 倍）——
        # 用 8 個 scale 重複到 16 個位置，等於隱含 recurrent sharing 卻沒寫明（Codex）。
        # 現在直接生成 n_slots 組，每組有自己的 K/V scale。
        def _buf(x, name):
            v = torch.as_tensor(x if x is not None else [1.0] * self.n_slots,
                                dtype=torch.float32)
            assert v.shape == (self.n_slots,), \
                f"{name} 需 {self.n_slots} 個值（layers×loops），收到 {tuple(v.shape)}"
            return v
        self.register_buffer("scale_k", _buf(scale_k, "scale_k"))
        self.register_buffer("scale_v", _buf(scale_v, "scale_v"))
        self.last_rms = None          # forward 時實測，不假設初始化後尺度不變
        out = n_layers * num_loops * 2 * n_kv_heads * head_dim
        self.net = nn.Sequential(
            nn.Linear(latent_dim, width), nn.GELU(), nn.Linear(width, out),
        )   # 標準初始化，不動

        inv = 1.0 / (rope_base ** (torch.arange(0, head_dim, 2).float() / head_dim))
        self.register_buffer("_inv_freq", inv, persistent=False)

    def _rope(self, k, cos, sin):
        """與 `model_minimind.apply_rotary_pos_emb` **逐位元相同**的公式。

        `cos`/`sin` 由呼叫端從 **core 自己的 `freqs_cos/sin` buffer** 切片傳入 ——
        adapter 自行重算只會覆蓋 default `rope_base`，
        core 若用自訂 theta 或 YaRN scaling 就不再是「同樣的旋轉」（Codex 指出）。
        """
        half = k.shape[-1] // 2
        rot = torch.cat((-k[..., half:], k[..., :half]), dim=-1)
        return (k * cos.unsqueeze(1) + rot * sin.unsqueeze(1)).to(k.dtype)

    def _fallback_freqs(self, n, device):
        ang = torch.arange(n, device=device)[:, None].float() * self._inv_freq[None, :].to(device)
        return torch.cat([ang.cos()] * 2, -1), torch.cat([ang.sin()] * 2, -1)

    def forward(self, latents, freqs=None, mask=None):    # (B, k, 25)
        B, k, _ = latents.shape
        o = self.net(latents).view(B, k, self.n_slots, 2, self.n_kv_heads, self.head_dim)
        # ⚠️ **架構約束：幅度被釘死（Codex 要求明列）。**
        #    先把每個 slot 的輸出正規化到單位 RMS，再乘上該位置的 native scale。
        #    只乘 scale 是不夠的 —— 網路原始輸出的 RMS 遠小於 1，
        #    實測所有位置一起偏低 11 倍（ratio ≈ 0.09），而 CV 只有 0.005 看起來完美。
        #    代價：**amplitude channel 被移除**，模型不能用「這條記憶比較強」表達任何東西。
        #    G1a 接受這個穩定化；若日後要放開，需另立條件並重測。
        rms = o.detach().float().pow(2).mean(dim=(0, 1, 4, 5), keepdim=True).sqrt()
        o = o / rms.clamp_min(1e-6).to(o.dtype)
        sk = self.scale_k.view(1, 1, -1, 1, 1).to(o.dtype)
        sv = self.scale_v.view(1, 1, -1, 1, 1).to(o.dtype)
        o = torch.cat([o[:, :, :, :1] * sk.unsqueeze(3), o[:, :, :, 1:] * sv.unsqueeze(3)], dim=3)
        if freqs is None:
            cos, sin = self._fallback_freqs(k, latents.device)
        else:
            cos, sin = freqs[0][:k].to(latents.dtype), freqs[1][:k].to(latents.dtype)
        # exact-zero 的語意必須釘住：normalize-then-scale 會讓**全零輸入照樣產生
        # 滿幅度的 KV**（實測 RMS 恰等於 target）—— 那是「憑 scale 生成假記憶」。
        # 缺項必須真的是零，不能被 normalization 復活（Codex）。
        if mask is None:
            mask = (latents.abs().sum(-1) > 0)
        m = mask.to(o.dtype).view(B, k, 1, 1, 1, 1)
        o = o * m
        slots = [(self._rope(o[:, :, i, 0], cos, sin), o[:, :, i, 1])
                 for i in range(self.n_slots)]
        # forward 時實測輸出 RMS，供對照 native calibration。
        # 只在初始化時對一次尺度、之後假設它保留，是站不住的（Codex）。
        # **必須 detach** —— 診斷欄位不該持有 autograd graph。
        with torch.no_grad():
            rk = torch.stack([kk.detach().float().pow(2).mean().sqrt() for kk, _ in slots])
            rv = torch.stack([vv.detach().float().pow(2).mean().sqrt() for _, vv in slots])
            self.last_rms = {
                "K": rk, "V": rv,
                "ratio_K": rk / self.scale_k.clamp_min(1e-8),
                "ratio_V": rv / self.scale_v.clamp_min(1e-8),
                "finite": bool(all(torch.isfinite(t).all() for kv in slots for t in kv)),
            }
        # cache 條目數是 layers × num_loops（model 以 past_kv_idx 跨 loop 遞增索引）。
        # 每個位置**各自生成**，不是同一批重複 —— native RMS 同層跨 loop 就不同。
        return slots


# --------------------------------------------------------------------------- 三種交付

class InlineTokensDelivery:
    """把值展開成 token 接在使用處。

    ⚠️ 這是 **positive control，不是 latent delivery**（規格 §5）——
    不能拿它來論證 latent 路徑成立。
    """
    is_latent_path = False


class LatentSlotsDelivery(nn.Module):
    """必須是 `nn.Module`（Codex 指出）——
    否則 adapter 不會進 `state_dict` / optimizer / `.to(device)`。"""
    is_latent_path = True

    def __init__(self, adapter: LatentSlotAdapter):
        super().__init__()
        self.adapter = adapter

    def apply(self, ws: ActiveWorkspace, latents, mask) -> ActiveWorkspace:
        carriers = self.adapter(latents)
        carriers = carriers * mask.unsqueeze(-1).to(carriers.dtype)   # 缺項歸零
        return ActiveWorkspace(hidden=ws.hidden, kv=ws.kv,
                               carriers=carriers, carrier_mask=mask)


class SyntheticKVDelivery(nn.Module):
    is_latent_path = True

    def __init__(self, adapter: SyntheticKVAdapter):
        super().__init__()
        self.adapter = adapter

    def apply(self, ws: ActiveWorkspace, latents, mask, freqs=None) -> ActiveWorkspace:
        # mask 先前被完全忽略。G1 的 selection 是 oracle 且必須全 support，
        # 這裡明確 assert，不做靜默的部分交付。
        assert bool(mask.all()), "SyntheticKV 在 G1 要求全 support；缺項需另行實作遮罩"
        synth = self.adapter(latents, freqs, mask)
        if ws.kv is None or all(x is None for x in ws.kv):
            kv = synth
        else:
            # zip 會靜默截短 —— 長度不符時要當場爆，不要產生一個長度較短、
            # 看起來正常的 cache（Codex 指出）。
            assert len(ws.kv) == len(synth), \
                f"cache 條目數不符：workspace {len(ws.kv)} vs synth {len(synth)}"
            assert not any(x is None for x in ws.kv), "workspace kv 有部分 None，無法合併"
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
