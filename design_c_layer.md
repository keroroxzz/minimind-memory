# C 層設計規格（G1 vertical slice）

2026-08-03。由 `research.md` §6 的閉環與 §4.12–4.21 的實測導出。
**這份是規格，不是計畫書** —— 每一條要嘛有實測支撐，要嘛明確標為未測的設計選擇。

## 0. 範圍：只做 G1，不蓋完整系統

**G1 = 一條可執行的 vertical slice：**

```
oracle-selected value → position-neutral latent → learned delivery → composition
```

| 這次做 | 這次不做 |
|---|---|
| 固定的 oracle selection（不學）| semantic / ANN retrieval |
| 小型固定 store（不學 write policy）| utility / eviction |
| learned delivery（latent → core 可用形式）| learned consolidate / merge |
| explicit-value baseline 對照 | million-scale |
| | 獨立可訓練的 WM 層（§4.20 無證據要求）|

**為什麼要固定前後兩端**：retrieval、write、synthesis 若同時可學，
失敗時無法定位是哪一段壞掉。§2.6 與 §4.19 都吃過這個虧。

## 1. 四個型別（最小集合）

以 29M backbone 為準：`hidden=512`、`layers=8`、`heads=8`、`kv_heads=2`、`head_dim=64`。

```python
ActiveWorkspace          # 共享暫態，不是層（§6）
    hidden:   (B, T, 512)          # loop 之間攜帶的 state
    kv:       list[8] of (B, 2, T, 64)   # recent native KV
    carriers: (B, M, 512)          # 已交付的 memory carrier，M 為 slot 數

MemoryEntry
    address:  (D_addr,)            # 比對用；G1 為 exact-key one-hot/embedding
    latent:   (25,)                # 內容；見下方「latent 的定義」
    metadata: dict                 # source / time / version
    version:  int

LatentStore                        # 只管持久化與物理 commit，無策略
    read(addresses)  -> list[MemoryEntry]      # snapshot 語意
    commit(entries)  -> None                   # 切斷跨 episode 的 graph
    # 模型不吐 CRUD；這是 backend 介面（§6）

MemoryInterface                    # Controller 是它的政策面，不另算一層
    query(workspace)      -> (addresses, support)
    select(candidates)    -> (entry | None, support: float)   # 必須唯一化
    deliver(entry, workspace) -> Delivery
```

### latent 的定義（G1 必須先固定，否則失敗無法歸因）

**G1 的 latent 是資訊完備、固定、canonical 的**，**沒有 content encoder**：

```
S₅ 置換 → 5×5 permutation matrix → flatten → (25,) one-hot
```

（等價選項：120 類 one-hot。取 25d 是因為它保留位置結構，便於日後接 encoder。）

⚠️ **不可在 G1 用 learned／compressed latent。** 若 latent 本身會丟資訊，
失敗就分不清是「latent 丟了資訊」還是「delivery 沒學會」。
**learned/compressed latent 另立 G1b，不得混入 G1。**

## 2. Delivery 保持抽象

**不可寫死** JIT／token inline／cross-attention 任一種。
理由：§4.15 已撤回「必須串流交付」，§4.17 的 value-vs-pointer 仍未定。

```python
class Delivery(Protocol):
    def apply(self, ws: ActiveWorkspace) -> ActiveWorkspace: ...

#   InlineTokens   把值展開成 token 接在使用處  ← positive control，**不是** latent delivery
#   LatentSlots    寫進 carriers，由 core 以 attention 讀取
#   SyntheticKV    直接合成 KV 條目插進 ws.kv
```

## 3. 梯度邊界

| 邊界 | 規則 |
|---|---|
| `LatentStore.commit` | **切斷 graph**。跨 episode 不回傳梯度 |
| `select` | G1 為 oracle，**無梯度** |
| store 內容 | **凍結**，不學 write |
| `deliver` | **可學** —— G1 唯一的可學組件 |

### G1a / G1b 必須分開預登記（先前規格自相矛盾）

原本同時寫了「唯一可學的是 delivery」與「梯度回傳到 core」——
**這兩句不能並存**：梯度若進 core，core 就也在學。分成兩階段：

| 階段 | core weights | 測什麼 |
|---|---|---|
| **G1a** | **凍結** | **plug-compatibility** —— 既有 core 能否直接吃 delivery 出來的東西 |
| **G1b** | 可與 delivery 共同適應 | **existential feasibility** —— 這條路徑到底行不行得通 |

**先跑 G1a。** 只有 G1a 不過才跑 G1b，且結論範圍不同
（G1a 過 = 可插拔；只有 G1b 過 = 需要重訓 core）。

### 「切換 delivery 不改 core 參數」的精確意思

指 **architecture 與 parameter count 不變**。
**weights 是否凍結由階段決定**：G1a 凍結、G1b 不凍結。
先前文字把兩者混用。

## 4. Invariants（要有測試）

1. **關閉記憶時與現有模型 bit-compatible** —— 舊 checkpoint 載入後輸出逐位元相同
2. **store 容量改變不改 core 參數量** —— 這是「記憶可獨立擴容」的操作型定義（§6）
3. **entry update / version 立即可見** —— 寫入後同一 episode 內讀得到新值
4. **commit 切斷跨 episode graph** —— 反向傳播不穿過 episode 邊界
5. **top-1 / support 有明確的空結果** —— 查不到時回 `(None, support)`，不是靜默回垃圾
6. **三種 Delivery 可互換** —— 切換不需改 core 參數
7. **所有新增 config 進 fingerprint 與 sidecar，且拒絕覆蓋** —— 今天已被同一個 bug 咬三次

## 5. G1 驗收標準

**任務**：沿用 `absent p=0 @ k≤24`（下游資料 checksum `7f81e60eca1e6b53`），
但值改由 store 交付，而非寫在 prompt 裡。

### baseline 必須由同一批樣本 paired render，不可借歷史數字

⚠️ **不可直接引用舊 `inline` 的 99.8%** —— `absent p=0` 與舊 `inline` 的
prompt/latent 分布不同。正確做法：

從**同一批 canonical `absent` 樣本**產生所有條件，逐題配對、hash 驗證：

| 層級 | render | 說明 |
|---|---|---|
| **L0** | oracle 把同一 chain 解成 explicit values | positive control |
| **L1/L2** | **只把值換成 latent**，其餘 prompt 與答案完全相同 | 受測條件 |

### 分級通過條件（取代原本的「三選二」）

`InlineTokens` 是 **positive control，不是 latent delivery** ——
不能拿它湊數（Codex）。改成分級：

| 層級 | 條件 | 可宣稱 |
|---|---|---|
| **L0** | explicit control 達標 | **必過**，否則測試台本身有問題 |
| **L1** | `LatentSlots` **或** `SyntheticKV` 任一過 | **latent delivery 存在性成立** |
| **L2** | `SyntheticKV` 過 | 使用者目標路徑成立 |
| **L3** | 兩種 latent 路徑都過 | implementation generality |

**單一路徑成功是有效結論**，只是範圍綁在該實作上。

**達標定義**：與 L0 的配對 95% CI 差值 **不低於 −5pp**，
且 **k=1 ≥ 95%**。

⚠️ **k=1 這道閘在這裡是 delivery/execution sanity gate，不是 binding gate**
—— selection 已經是 oracle，這裡根本沒在測 binding（Codex）。
我先前從 §4.10 抄了「binding gate」的標籤，那是錯的。

### 失敗的判讀範圍

**單一 adapter 失敗** → 只否定**該 latent + delivery + training protocol** 組合。

**但不可寫成不可反駁（Codex）**：若在
「lossless latent + small-batch overfit 通過 + 合理容量/初始化」的條件下，
**兩條 latent 路徑都失敗**，那就應該否定 G1 的前提 ——
**「此 backbone 能直接吃 position-neutral latent」**。

此時 §6 的閉環仍可退回「解碼成 explicit value 再交付」，
但 **synthetic-latent 分支被實質削弱**。

## 6. 分工

檔案由 Claude 主責，Codex 負責 theory / review，避免同檔衝突。

## 7. 這份規格背後的實測依據

| 條 | 來源 |
|---|---|
| 選擇必須外部唯一化，≤2 是核心的 fallback 容錯 | §2.6（4 候選 100%→4.7%，loop4 救不回）|
| 值交付優於指標交付（深處）| §4.17（k=1 100% vs 43%）|
| binding 不會從答案梯度自己長出來 | §4.12、§4.19（兩分布、兩 seed）|
| 需要 matching auxiliary，且它本身要學得起來 | §4.12、§4.14 |
| 該 auxiliary 是**可撤除的習得鷹架** | §4.20（撤除後 A_ans 100%）|
| **但棄答/support 校準撤了就沒了** | §4.20（R_abstain 0%）|
| 不必強制 JIT delivery | §4.15（`padded k≤4` = 100%）|
| WM 不需要獨立可訓練層 | §4.20、§8.1（E3 無可靠證據）|
