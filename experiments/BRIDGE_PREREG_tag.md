# 預先登記 — slot identity ladder（tag 階段）

**寫於 2026-08-06，在任何 tag 實驗跑之前。** 依 Codex [136] 的 ladder 逐條落實。
`research.md` §4.53 已定位問題：**原生定址被交付蓋掉（必要條件），但 attention
不是選擇機制**，所以下一步是**顯式 slot identity／binding channel**。

**舊結論不回寫**：H2 binding-loss 與 H3 UNDECIDABLE 維持原樣，本文件是**新 prereg／新 checkpoint**。

---

## 0. 為什麼要分兩臂（這是 Codex 修正我的地方）

我原本只提「把 slot 身分編進交付」。Codex 指出那**只測身份能否被保留**，
**不解決語意 binding**：query 若沒有可比對的 address，模型仍不知道
哪個物理 tag 對應被問的 entity。因此**兩臂，不得合併成一個 PASS**：

| 臂 | tag 從哪來 | 測什麼 |
|---|---|---|
| **A `physical-tag`** | **物理 slot 序號**（或交付前就決定的隨機置換） | **身份保存** —— 交付後「我是第幾格」還在不在 |
| **B `address-tag`** | 由該條記憶的 **entity/key 決定性生成**；**query 獨立生成同一 address** | **語意綁定** —— 靠 address 比對找到正確那條 |

B 才是目標系統要的（query 用描述定址，不是用格子編號）。
**A 過了不代表 B 會過；A 沒過則 B 幾乎不可能過。**

---

## 1. tag 為什麼不是「oracle 把答案送到嘴邊」（Codex [136] 的三個條件）

tag 必須滿足**全部**三條，否則本實驗無效：

1. tag 在 **episode 生成／交付之前**就由 physical slot 或隨機置換決定，
   與 `used_id`、query target、答案、值內容**統計獨立**。
2. **每一條 delivered carrier 都拿同規格的 tag** —— 不得只給被問的那格。
3. test 時**隨機換位**（tag↔物理位置的對應重抽）。

**違反即無效**：若 tag 由 target-conditioned writer 產生，或只發給被問的 slot，
那就是把答案送進去，結果不得採信。

> tag 只說「**我是第幾個載體**」，不說「**你要讀我**」。

---

## 2. A 臂（`physical-tag`）規格

- 交付改為 `f(z, tag)`：`tag` 是**固定正交碼**（維度鎖 8，交付前生成，永不訓練）。
  core **完全凍結**，只訓 delivery，架構／資料／步數與 `_f8` 對齊（12000 步、bs 32、`nfreq=8`）。
- 另跑 **oracle 小過擬合**（small-overfit）確認 tag 通道本身可用：
  若 oracle 都學不起來，是實作錯誤，先修再談。

### 可判性門檻（沿用 H3 的教訓，先鎖再看）

**`n_delivered=1` 的正確率必須 ≥95%**，否則判 **INVALID / capacity-insufficient**，
**不得讀 `n≥2` 的斜率**。（`_f8` 這格是 99.6%，所以 tag 臂沒有理由做不到；
若做不到，代表 tag 通道排擠了內容通道，那本身是結論。）

### primary：**是否脫離 `1/n_delivered`**

`n_delivered ∈ {2,3,4}`，每格 n=250。primary 是 `exact`（對 `orig`）。

| 裁決 | 條件 |
|---|---|
| **A-PASS** | `n_del=2` 與 `n_del=4` 的正確率 95% CI **都排除 `1/n_del`**，且**都在其上方** |
| **A-FAIL** | 任一格的 CI **含 `1/n_del`**（仍在亂挑） |
| **INVALID** | `n_delivered=1` < 95% |

**`n_delivered=1` 的高分不算數** —— 只注一條時 binding 平凡（§4.53 已鎖）。

### 必跑的負對照（沿用 `BRIDGE_PREREG_shuffle.md`，不得省略）

- **內容 shuffle**：`cf` 應跟著送進去的值走；`orig` 應在地板 → 確認內容仍真被讀。
- **位置 shuffle**：**這是 A 臂的核心對照**。tag 若真的保存身份，
  錯排之後模型應該**跟著 tag 走**，而不是 `orig`/`cf` 各半。
  - 若 `cf` 顯著高於 `orig` → **身份被保留**（tag 有效）。
  - 若仍 `orig ≈ cf ≈ 1/n` → **A-FAIL**，tag 沒有被使用。

---

## 3. B 臂（`address-tag`）—— A 判讀完才開，不同時跑

- 每條記憶帶一個由其 **entity/key**（`name + attr`）**決定性生成**的 address；
  **query 端獨立用同一函數生成**同一 address。**不得注入 target label。**
- 判讀同 A，但**額外要求**：address 必須對**未見過的 entity** 也成立
  （沿用 G2c 的教訓：canonical encoder 的訓練必須涵蓋它會遇到的 tokenization 結構）。
- **A 與 B 分開記錄，不得合併成一個 PASS。**

---

## 4. C 臂（`norm-constrained`）—— 對照，不是修法

Codex [136] 同意這**不是**被禁的 scale 介入：它是**新訓練工作點／新 checkpoint**，
不是在已學參數上事後 sweep。但規則必須**事前固定**：

- 約束 `‖ΔK‖ ≤ c·‖K_nat‖`，**逐 layer／slot** 施加。
- **`c` 由獨立 calibration 或數值穩定規範鎖定，不得看 test 選 `c`。**
- 同架構、同資料、同步數，與 unconstrained baseline **配對**。

**它只能作「native address 保留」的控制，不替 slot identity 提供必要條件。**

判讀（Codex 事前鎖）：

- 約束**降低淹沒但仍貼 `1/n`** → **attention 方向不是唯一問題，binding channel 仍必要**。
- 約束**同時脫離 `1/n`** → 才支持「淹沒＋identity 保留」這條路徑。

⚠️ **不得預設 norm 修法一定會改善選擇** —— §4.53 的 attention permutation null 是**陰性**
（p=0.16，沒有任何 slot 在做選擇），所以沒有理由預期只還原範數就會讓選擇變對。

---

## 5. 執行順序（不可跳階，Codex [136]）

1. **A `physical-tag`**（frozen delivery，oracle small-overfit + learned），
   要求 `n=1` consumer ceiling 與 `n≥2` shuffle。
2. **B `address-tag`**。
3. **C `norm-constrained` × tag factorial**。

**tag、norm、writer/retriever 各自新 prereg。**
