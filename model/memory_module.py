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
    address: str                       # 符號位址（'f0' 等）
    latent: torch.Tensor               # (LATENT_DIM,)
    metadata: dict = field(default_factory=dict)
    version: int = 0
    addr_vec: torch.Tensor | None = None   # 檢索用的 address 向量（retrieve 階段）


ADDR_DIM = 32


_ADDR_CACHE = {}


def address_bank(n: int = 16, dim: int = ADDR_DIM) -> torch.Tensor:
    """n 個**互相正交**的固定 address（n ≤ dim）。

    隨機單位向量在 32 維下 pairwise |cos| 約 0.18，最壞的一對可能更高 ——
    那會讓「檢索失敗」與「兩個 address 太像」混在一起（Codex 要求檢查 margin）。
    正交化直接把這個變因移除。
    """
    key = (n, dim)
    if key not in _ADDR_CACHE:
        assert n <= dim, f"{n} 個正交向量需要 dim ≥ {n}"
        g = torch.Generator().manual_seed(20260804)     # 固定 seed，跨 process 穩定
        q, _ = torch.linalg.qr(torch.randn(dim, n, generator=g))
        _ADDR_CACHE[key] = q.T.contiguous()             # (n, dim)，列正交
    return _ADDR_CACHE[key]


def address_vector(symbol: str, dim: int = ADDR_DIM) -> torch.Tensor:
    """`f<i>` → 第 i 個正交 address。**固定、不可訓練。**

    retrieve 階段刻意不訓練 address —— 若 query 與 address 一起學，
    兩者會共同漂移成任意編碼，那測到的就不是「能不能找到對的那條」，
    而是「能不能自己約定一套暗號」。
    """
    i = int(symbol[1:])
    return address_bank(max(16, i + 1), dim)[i]


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


class Retriever(nn.Module):
    """query/address/selector —— retrieve 階段**唯一可學**的組件。

    query 由該 step 位置的線索算出，與 store 的 address 做內積 → top-1。
    另有一個 support head 產出 hit/miss 分數：§4.12/§4.20 已證實
    **只靠答案梯度時 binding 不會湧現**，所以 hit/miss 要直接監督。
    """

    def __init__(self, in_dim: int, addr_dim: int = ADDR_DIM, width: int = 256):
        super().__init__()
        self.q = nn.Sequential(nn.Linear(in_dim, width), nn.GELU(), nn.Linear(width, addr_dim))
        # support **必須由檢索 logits 產生，不能只看 cue**。
        # 「這個 key 在不在 pool 裡」取決於 pool，不是 key 本身 ——
        # 只看 cue 的 head 拿不到判斷所需的資訊，實測退化成永遠預測多數類
        # （threshold 校到搜尋邊界、準確率恰等於 hit 的基準率）。
        # 用 top-1、top-2 與 logsumexp：那是「有沒有找到好匹配」的自然訊號。
        self.support = nn.Sequential(nn.Linear(3, 32), nn.GELU(), nn.Linear(32, 1))
        self.log_temp = nn.Parameter(torch.zeros(()))

    def forward(self, cue, addr_bank):
        """cue (B,k,in_dim)；addr_bank (B,N,addr_dim) → logits (B,k,N)、support (B,k)。"""
        q = torch.nn.functional.normalize(self.q(cue), dim=-1)
        a = torch.nn.functional.normalize(addr_bank, dim=-1)
        logits = torch.einsum("bkd,bnd->bkn", q, a) * self.log_temp.exp()
        top2 = logits.topk(2, dim=-1).values
        feat = torch.stack([top2[..., 0], top2[..., 0] - top2[..., 1],
                            logits.logsumexp(-1)], dim=-1)
        return logits, self.support(feat).squeeze(-1)


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


class InlineLatentAdapter(nn.Module):
    """latent (25,) → **5 個 embedding**，塞回原本 value 所在的那 5 個位置。

    這是 Codex 設計的 ladder 中間階：
      `OracleInlineEmbedding`（零參數，同位置換原生 embedding）→ 已驗 100%
      → **本類**（learned，同位置）
      → prefix / KV（learned，異位置）→ 實測 3.3% / 21.7%

    若本類通過而 prefix/KV 不過，才支持「交付**位置**」假說；
    若本類也不過，問題就在「latent 能否被表達成 core 可用的 embedding」。
    """

    def __init__(self, hidden_size: int, span: int = PERM_N,
                 latent_dim: int = LATENT_DIM, width: int = 512):
        super().__init__()
        self.span, self.hidden = span, hidden_size
        self.net = nn.Sequential(
            nn.Linear(latent_dim, width), nn.GELU(), nn.Linear(width, span * hidden_size),
        )

    def forward(self, latents):                  # (B, k, 25) -> (B, k, span, H)
        B, k, _ = latents.shape
        return self.net(latents).view(B, k, self.span, self.hidden)


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


class StaticFullKVAdapter(nn.Module):
    """3A static-full：**每個 loop×layer 一個獨立 head**，輸入**只有 latent**。

    與 `SyntheticKVAdapter` 的差別：後者用一個共享 MLP 產生全部 slot；
    這裡每個 slot 有自己的非線性 head，容量與獨立性都更高。

    ⚠️ **輸入不得偷加 context**（Codex）—— 這正是 3A 要測的變因。
    若它通過 → 先前的失敗是**共享容量**不足；
    若它失敗 → 只能說「高容量、逐位置但 **context-free** 的 KV 合成失敗」，
    **不可**升級成「所有合成 KV 不可組合」。
    """

    def __init__(self, n_slots: int, n_kv_heads: int, head_dim: int,
                 latent_dim: int = LATENT_DIM, trunk: int = 256, head: int = 256,
                 span: int = PERM_N, scale_k=None, scale_v=None, rope_base: float = 1e6):
        super().__init__()
        self.n_slots, self.n_kv_heads, self.head_dim = n_slots, n_kv_heads, head_dim
        # 每個 latent 對應 **span 個 token 位置**（value span），
        # 不是一個 slot —— 同位置注入要覆蓋那 5 個位置各自的 K/V。
        self.span = span
        self.trunk = nn.Sequential(nn.Linear(latent_dim, trunk), nn.GELU())
        out = span * 2 * n_kv_heads * head_dim
        self.heads = nn.ModuleList([
            nn.Sequential(nn.Linear(trunk, head), nn.GELU(), nn.Linear(head, out))
            for _ in range(n_slots)])

        def _buf(x):
            v = torch.as_tensor(x if x is not None else [1.0] * n_slots, dtype=torch.float32)
            assert v.shape == (n_slots,)
            return v
        self.register_buffer("scale_k", _buf(scale_k))
        self.register_buffer("scale_v", _buf(scale_v))

    def forward(self, latents):                  # (B,k,25) -> list[n_slots] of (K,V)
        B, k, _ = latents.shape
        t = self.trunk(latents)
        out = []
        for i, h in enumerate(self.heads):
            o = h(t).view(B, k * self.span, 2, self.n_kv_heads, self.head_dim)
            # 與 SyntheticKV 相同的硬式尺度校準（見 §3.6：幅度通道被移除）
            rms = o.detach().float().pow(2).mean(dim=(0, 1, 3, 4), keepdim=True).sqrt()
            o = o / rms.clamp_min(1e-6).to(o.dtype)
            kk = o[:, :, 0] * self.scale_k[i].to(o.dtype)
            vv = o[:, :, 1] * self.scale_v[i].to(o.dtype)
            m = (latents.abs().sum(-1) > 0).to(o.dtype)
            m = m.repeat_interleave(self.span, dim=1)[:, :, None, None]
            out.append((kk * m, vv * m))         # exact-zero 保持零
        return out


class ContextualKVAdapter(nn.Module):
    """3B contextual in-place KV：`G_{loop,layer}(z_i, native_kv_i)`。

    3A（context-free，6.32M、16 獨立 head）在 k=1 達 98% 但 k=4 只有 3% ——
    容量不是瓶頸。Codex 的假說：合成器缺少 **state 與前序 value 的條件**，
    而 Inline embedding 會在每層被更新、teacher KV 正是那些 contextual hidden 產生的。

    這裡讓合成器看得到**該層在該位置算出來的 native K/V**（已含 context），
    輸出 **delta**：`K' = K_native + ΔK(z, K_native)`。
    零初始化最後一層 → 起始時等同不交付，訓練才逐步偏離。
    """

    def __init__(self, n_slots: int, n_kv_heads: int, head_dim: int,
                 latent_dim: int = LATENT_DIM, width: int = 256, span: int = PERM_N,
                 use_context: bool = True, residual: bool = True):
        super().__init__()
        self.n_slots, self.n_kv_heads, self.head_dim, self.span = \
            n_slots, n_kv_heads, head_dim, span
        # 2×2：3B 同時改了「加入 context」與「改成 residual」兩件事，
        # 所以只能宣稱 bundle 成功。這兩個旗標補上缺的兩格（Codex）：
        #   use_context=False, residual=True  → z-only delta
        #   use_context=True,  residual=False → contextual absolute
        self.use_context, self.residual = use_context, residual
        d = n_kv_heads * head_dim
        self.lat = nn.Linear(latent_dim, width)
        self.ctx = nn.Linear(2 * d, width)
        self.heads = nn.ModuleList([nn.Linear(width, 2 * d) for _ in range(n_slots)])
        for h in self.heads:                       # 零初始化 → 起始 delta = 0
            nn.init.zeros_(h.weight); nn.init.zeros_(h.bias)

    def forward(self, slot, latents, k_nat, v_nat):
        """latents (B,k,25)；k_nat/v_nat (B,P,n_kv,hd)，P = k*span。"""
        B, P = k_nat.shape[0], k_nat.shape[1]
        d = self.n_kv_heads * self.head_dim
        z = self.lat(latents).repeat_interleave(self.span, dim=1)       # (B,P,W)
        if self.use_context:
            z = z + self.ctx(torch.cat([k_nat.reshape(B, P, d),
                                        v_nat.reshape(B, P, d)], -1))
        o = self.heads[slot](torch.nn.functional.gelu(z))
        ok, ov = (o[..., :d].view(B, P, self.n_kv_heads, self.head_dim),
                  o[..., d:].view(B, P, self.n_kv_heads, self.head_dim))
        if self.residual:
            return k_nat + ok, v_nat + ov
        # absolute override：頭是零初始化的，純 absolute 會輸出全零 →
        # 這裡改用標準初始化的等價路徑（見 g1_train 建構處會重新初始化）
        return ok, ov


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
