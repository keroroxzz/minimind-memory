# 預先登記 — 橋接任務：shuffled 負對照 + 連續 schema 指標

**寫於 2026-08-05 21:0x，`bridge_delivery.py --mode v_only` 仍在跑（PID 4192548），
本文件在看到 `_f8v` 任何結果之前鎖定。** 對照 Codex #130 問2（指標）與問3（shuffle）。

這份文件鎖三件事：指標、判讀門檻、對照的定義。之後不得事後更改。

---

## 0. 為什麼要做

`results_bridge_diag_ncarrier_f8.json` 量到 `n_carrier=1 → 100.0%`（n=119）。
在解釋成「單載體 delivery pipeline PASS」之前，必須排除兩條捷徑：

- **內容沒有被讀**：core 從模板／位置猜答案，latent 只是裝飾。
- **綁定沒有被讀**：多載體時 core 不需要知道「哪一條 latent 屬於哪一格」。

⚠️ **先寫下一個已知的削弱**：`make_item` 用 `force_used=True`，所以 `j=0` 且
`n_carrier=1` 時那唯一的載體**必然就是被問的那格**。此時**綁定是平凡的**。
因此 `n_carrier=1` 最多只能宣稱「**無需綁定的內容輸送**」，不能宣稱綁定成立。
綁定要靠 `n_carrier>=2` 的位置 shuffle 來測。這句話現在寫下，不論結果如何都算數。

---

## 1. 連續 schema 指標（鎖死）

答案是 0..99 的整數，render 成空格分隔兩位數。模型輸出解析：`strip().split()`，
恰好 2 段且都是單一數字 → `pred = 10*a + b`；否則記 **`parse_fail`**。

primary 與 secondary 的分工：

| 指標 | 定義 | 角色 |
|---|---|---|
| `exact` **對 `orig` 評分** | `pred == 原答案`（`parse_fail` 算錯） | **primary** |
| `exact` **對 `cf` 評分** | `pred == 反事實答案` | **secondary**（見修訂 2）|
| `MAE` | `mean(\|pred − truth\|)`，只在可解析樣本上 | secondary |
| `RMSE` | `sqrt(mean((pred − truth)^2))`，同上 | secondary |
| `P(\|err\|<=eps)` | **eps ∈ {0, 1, 2, 5}**，值域 0..99，**現在鎖死** | secondary |
| `P50 / P90 / P99` | `\|err\|` 的分位數 | secondary |
| `signed_bias` | `mean(pred − truth)` | secondary |
| 逐座標 | 十位正確率、個位正確率（CLAUDE.md 硬規則） | secondary |
| `parse_fail` | 未輸出合法兩位數的比例 | 必報 |

**不得事後新增 eps 或改門檻把失敗講成成功。** `MAE`/`RMSE` 只在可解析樣本上算，
所以必須與 `parse_fail` 一起讀；單看 MAE 是可以被 `parse_fail` 洗掉的。

---

## 2. 兩個對照的定義

共用：同一批 episode、同一個 `make_item`（`p=0.6, force_used=True`）分布、
**新 seed（`SEED=20260805`）**、逐 episode reset、以 `ep.eid` 去重（不得重複題目）。

每個 cell 都同時對兩個目標評分：

- `orig` = 原始答案。
- `cf` = **反事實答案** —— 用「這次實際被送進去的值」重算 `x <- |x - v|` 鏈。
  非載體的 fact 一律用文字裡的值（沒被動到）。

自我檢查（硬 assert，失敗就中止）：用**真值**重算鏈必須逐題等於 `ep.answer`。
鏈是從 query 字串反解的，這個 assert 是它唯一的正確性保證。

### 對照 A — 內容 shuffle（位置固定，打亂 latent↔fact 綁定）

載體位置與 mask **完全不動**；每個載體格送進去的 latent，其**值**換成一個
外來的隨機值（donor RNG，與原值不同，且維持「全部值互異」這個生成器不變量）。

**屬性 one-hot 維持與文字一致，故意不動** —— 若連屬性都換掉，失敗可以被歸因成
「latent 自相矛盾所以 core 拒答」，那就測不到內容輸送。

判讀（j=0，`n_carrier=1`，這是要救的那格）：

- `cf` 高（≈ intact） → **內容真的被讀**，`n_carrier=1` 的 100% 成立。
- `cf` 塌到 `nodeliver` 水準、而 `orig` 仍高於 chance → **core 讀的是模板／位置**，
  **`n_carrier=1 → 100%` 這個數字被推翻**。
- 兩者都塌 → 交付對外來值不泛化（值域外插問題），另記。

### 對照 B — 位置 shuffle（內容固定，隨機化合法 slot）

**只在 `n_carrier >= 2` 定義**（1 條載體無法錯位）。把該題自己的載體 latent
在自己的載體位置之間做一個**錯排（derangement）**，內容集合完全不變。

判讀：`cf` 高 → core 依位置綁定，交付有效；`orig` 高於 chance → 位置捷徑
（答案與擺哪無關），**那會推翻多載體結果的解釋方向**。

### 每個對照的臂

| 臂 | 意義 |
|---|---|
| `nodeliver` | 完全不交付（". ." 留在文字裡）—— **經驗地板**，chance 由它定義，不用理論值 |
| `oracle` | oracle inline（零參數）—— 用「實際送進去的值」的 token embedding |
| `learned` | 受測的 delivery（`_f8`，`nfreq=8`，`mode=kv`） |

`oracle` 臂同樣要做 shuffle：它是「若 delivery 完美」的上界。
若 `oracle` 在 shuffle 下也塌，問題在**任務／core**，不在 delivery。

---

## 3. 判讀門檻（鎖死）

以 `intact` 的同格 `exact` 為參照，Wilson 95% CI 不重疊才算差異：

1. **內容真被讀** ⇔ 內容 shuffle 的 `cf exact` 與 `intact exact` 差距 **≤5pp**
   （沿用 Codex #126 對 text ceiling 用的同一個 5pp 門檻），且
   `orig exact` 與 `nodeliver` 的 `orig exact` 無顯著差異。
2. **有位置捷徑** ⇔ 位置 shuffle 的 `orig exact` 顯著高於 `nodeliver`。
3. 以上任一在 `oracle` 臂就失敗 → 該格**不可用來裁決 delivery**，先修任務。

**不得因為結果難看就改判讀方向。** 若 1 不成立，`n_carrier=1 → 100%` 這個數字
在 `research.md` 與 `BRIDGE_NEXT.md` 裡都要標成**已推翻**，不是「有待釐清」。

### 修訂 1（`n=8` smoke 之後、正式 `n` 之前，只看機制不看結果）

`j>=1` 時 `|x−v|` 的鏈可能讓 `cf` 與 `orig` **剛好相同**。這種題目答對 `cf`
必然同時答對 `orig`，會把 `orig` 那一欄灌水，讓「位置捷徑」看起來成立。
因此**判讀 2（位置捷徑）一律用排除 `cf == orig` 之後的 `exact_nc`**，
並同報該子集的 n。`j=0` 因生成器保證同題值互異，不會碰撞，兩者相同。

發現途徑寫明：smoke 時 `j=1` 的 **oracle** 臂 `orig` 是 50%，而 oracle 的 `cf`
是 100% —— oracle 交付的必然是被替換的值，不可能有捷徑，所以那 50% 只能是碰撞。
是**對照本身**抓到量測缺陷，不是看主結果調整。

### 修訂 2（Codex [131] 問1 的裁決，正式結果落地之前）

我原本想把 `cf` 當 primary，**Codex 否決一半**：`orig` 維持 prereg **primary**，
`cf` 是**合法且必要的 secondary**（它回答「模型是否跟隨實際送進去的內容」），
但不得取代 `orig` 的 primary 地位。正式表**三欄同報**：`orig`、`cf`、`exact_nc`。

判讀鎖成三分：

| 觀察 | 判讀 |
|---|---|
| `cf` 高、`orig` 低 | **內容被讀，但綁定錯** |
| 兩者皆低 | **delivery / consumer 失敗** |
| `orig` 高、`cf` 低 | **位置／模板捷徑，或有洩漏** |

**`oracle` 臂的 `cf`=100% 只驗證反事實答案的構造是對的，不替 `learned` 臂背書。**
（smoke 已見 oracle 六格全 100%，所以構造已驗；這不是 delivery 的成績。）

---

## 4. 樣本數

每個 (j, control, arm) **n = 250** 題（去重後）。
`exact` 在 0.5 附近的 95% CI 半寬約 ±6pp，足以判 5pp 以外的差距；
在 0.95 附近約 ±2.7pp。這是預先接受的解析度，事後不加樣本。

`j ∈ {0, 1}`。**`j>=2` 一律 censor（gate 1：`L0` < 95%）**，不跑。
