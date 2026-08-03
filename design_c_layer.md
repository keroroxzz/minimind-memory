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
    latent:   (D_lat,)             # 內容；position-neutral
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

## 2. Delivery 保持抽象

**不可寫死** JIT／token inline／cross-attention 任一種。
理由：§4.15 已撤回「必須串流交付」，§4.17 的 value-vs-pointer 仍未定。

```python
class Delivery(Protocol):
    def apply(self, ws: ActiveWorkspace) -> ActiveWorkspace: ...

# G1 至少要能替換這三種實作，且切換不動 core 參數：
#   InlineTokens   把值展開成 token 接在使用處（= inline 條件）
#   LatentSlots    寫進 carriers，由 core 以 attention 讀取
#   SyntheticKV    直接合成 KV 條目插進 ws.kv
```

## 3. 梯度邊界

| 邊界 | 規則 |
|---|---|
| `LatentStore.commit` | **切斷 graph**。跨 episode 不回傳梯度 |
| `select` | G1 為 oracle，**無梯度** |
| `deliver` | **可學**，梯度回傳到 core |
| store 內容 | G1 **凍結**，不學 write |

**唯一可學的是 delivery。** 這是 G1 要量的東西。

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

| 對照 | 內容 |
|---|---|
| **baseline** | explicit value in prompt（= 現有 `inline`，已知 99.8%）|
| **G1** | oracle-selected → latent → learned delivery |

**通過條件（事前登記）：**

- G1 的 overall 與 baseline 的**配對 95% CI 差值不低於 −5pp**
- **k=1 ≥ 95%**（binding gate，沿用 §4.10）
- 三種 Delivery 至少**兩種**達標 —— 只有一種達標代表結論綁在該實作上

**失敗的判讀**：G1 低於 baseline 只證明**這個 latent→delivery 路徑**不足，
**不反駁** §6 的閉環 —— 因為 store 與 selection 都被固定住了，
失敗只可能出在 delivery 或 latent 表示。

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
