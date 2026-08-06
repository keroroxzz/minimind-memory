# 預先登記 — B 臂 `address-tag`（`[QADDR]` 獨立 query 側通道）

**寫於 2026-08-06，在任何 B 臂結果之前。** 規格由 Codex [137] 定死，本文件只落實。
A 臂的 **A-FAIL**（`research.md` §4.54）**不回寫**；這是新 prereg／新 artifact。

---

## 0. 為什麼不是 A 的重試

A 臂證明：**物理 slot 身份被消費了，卻完全沒參與選擇**
（tag 歸零與完整 tag 無差異；錯排後既不跟 tag 也不跟位置）。
所以**不能在同一臂上再加 tag 維度** —— 缺的不是身份，是
**query 端沒有可比對的 address**。

---

## 1. 設計（Codex [137]，我原本的 (a)/(b)/(c) 三案都被否）

- **原本的 `(name, attr)` 文字 token 完全不改。** 直接改寫 query token 會把
  **文字路徑與 address 路徑混在一起**，所以不採用我提的 (a) 原形。
- 新增一個專用的 **`[QADDR]` query 側 carrier／side-channel**。
- 共享 **`AddressEncoder φ`**（固定、未訓練）：
  - 每條 memory descriptor `d_i` → `a_i = φ(d_i)`
  - query descriptor `d_q` → `a_q = φ(d_q)`
- **memory slot 注入 `(a_i, z_i)`；`[QADDR]` 只注入 `a_q`。**
- `φ` 在 episode 前、memory retrieval 前計算，**與 target slot 獨立**。

**為什麼不是 oracle**：它提供「**問題在問哪個 descriptor**」，
**沒有**提供「**答案在哪個 slot**」。模型仍得自己把 `a_q` 與各個 `a_i` 比對。

- B 第一版用**精確 canonical descriptor**，**不宣稱 semantic retrieval**。
- **每 episode 隨機 slot permutation**，並測**未見過的 permutation**。
- **address shuffle 與 content shuffle 分開做。**

---

## 2. ⚠️ 先過 render gate，否則會把 OOD 誤判成 B FAIL

`[QADDR]` 是**新 token／新版面**，凍結 core **從未見過**。
§4.50 記過 G4b 的教訓：**只因為在 prompt 前後加了東西，oracle 天花板一度掉到 0.0%**。

> **Codex [137] 明令**：若凍結 core 無法消費新的 `[QADDR]` 流形，
> **必須按橋接 core 規格重訓一個含 `[QADDR]` render 的 core**；
> **不得把新 token 的 OOD 失敗誤稱為 B FAIL。**

**因此第一件事是 render gate（不訓練、純評估、很便宜）**：

| 條件 | 內容 |
|---|---|
| `L0` 無 `[QADDR]` | 現行 render，已知 j=0 為 100% |
| **`L0` 有 `[QADDR]` 空位** | 同一題，只是多了 `[QADDR]` 版面（**不注入任何東西**）|

**判讀（事前鎖）**：
- `L0` 有 `[QADDR]` 的 j=0 **≥95%** → **格式可用**，凍結 core 繼續，進 §3。
- **< 95%** → **新版面 OOD**，`bridge_core` **必須重訓**（含 `[QADDR]` render，
  train/val/test 全涵蓋），**在此之前不得跑 B 臂的任何 learned 條件**，
  更不得把結果寫成 B FAIL。

---

## 3. 可判性門檻（Codex [137] 問2，B 臂重開，不沿用 A 的 100%）

B 改動了輸入分布，`n=1` 的天花板**必須在同一個 B-render 上重量**。三個 ceiling：

| 臂 | 意義 |
|---|---|
| `text / L0` | 版面天花板 |
| `oracle-address` | address 通道的上界 |
| `learned-address` | 主條件 |

**`query-address` 臂的 `n=1` 必須 ≥95%**（或與**同格 oracle** 的 CI gap ≤5pp）
**才可以解讀 `n≥2`**。**`n=1` 仍然只算 content transport，不算 binding。**

---

## 4. binding primary

- 指標：`orig` / `cf` / `exact_nc`（定義沿用 `BRIDGE_PREREG_shuffle.md`）。
- 必跑：**address shuffle** 與 **random slot permutation**。
- **PASS 需同時滿足兩條**：
  1. **脫離 `1/n`**（`n_del=2` 與 `4` 的 CI 都排除且高於 `1/n`）；
  2. **`cf` 與 `orig` 分離**（不是兩邊都貼亂猜 —— A 臂正是敗在這裡）。

---

## 5. 必跑的 query-address ablation（Codex [137] 指定的負對照）

**只動 `[QADDR]`，memory 完全不動**：

| 條件 | 預期（若 address 真的被用）|
|---|---|
| 正確 `a_q` | 基準 |
| **`[QADDR]` 置零** | 應**變差**；若與正確**無差** → **core/adapter 根本沒用 address** |
| **`[QADDR]` 錯配**（給別的 descriptor 的 address） | 應落向 chance 或 `cf` |

> **若置零與正確無差，B 不是「差一點」，而是「未介入」** —— 這個區分必須明寫。

---

## 6. 順序

1. **render gate**（§2，不訓練）→ 決定要不要重訓 core。
2. `n=1` 三個 ceiling（§3）→ 決定 `n≥2` 能不能讀。
3. binding primary（§4）＋ ablation（§5）。

**tag、norm、writer/retriever 各自新 prereg；A 的 A-FAIL 與 H3 的 UNDECIDABLE 都不回寫。**
