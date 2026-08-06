# 橋接任務 — 交接（2026-08-06 更新）

## 最新狀態：**latent-only core 成立（j=0/1）** —— `research.md` §4.55

**方向已改**：placeholder bridge（本文件下半部）**只是診斷工具**，不是產品架構。
目標句改為 —— **latent-only query 在無文字事實、無 placeholder、無特殊 token 下仍能正確回答。**

| j=0 | n=2 | n=3 | n=4 |
|---|---|---|---|
| **新 core `latent`** | **99.2%** | **96.0%** | **95.2%** |
| 舊 core（無身份 schema） | 50.0% | 35.9% | 23.6%（貼 `1/n`）|
| `floor`（carrier 歸零） | 1.2% | 1.2% | 1.6% |

j=1 為 97.2 / 93.2 / 90.4%；**j=2 未過 gate（90.0%）→ censor**。

**兩個改變缺一不可**：(1) schema 補上身份 `z' = [φ(name,attr), value, attr]`；
(2) render 改成只有問句、記憶走 `memory_carriers`。

**決定性對照 `value_shuffle`**（值錯排、address 留原位）：`orig` **0.0%**、
`cf`（跟著 address）**95.2%** → **選擇由 address 驅動**。

⚠️ **一條事前判準按字面 FAIL**（`addr_zero` 應退回 `1/n`，實測 11.6% 低於 `1/n`）。
Codex #139 裁定**雙層記錄**：判準 FAIL 不得改寫，同時標為 mis-specified fallback
diagnostic；正向機制改依 `value_shuffle`/`addr_random`/`payload_zero`。
**不得宣稱「沒有 address 時會均勻亂挑」。**

**檔案**：`bridge_latent_schema.py`、`bridge_train_core_latent.py`、`bridge_latent_gate.py`、
`bridge_latent_shuffle.py`、`bridge_core_latent.pth`、`BRIDGE_PREREG_latent.md`。

### 現況（2026-08-06 收尾）

| 軸 | 狀態 |
|---|---|
| §4.55 latent-only address 選擇 | **成立**，但**只適用「每個實體最多一條記憶」**（§4.57 補的範圍限制）|
| A 路 retrieval ＋ typed guard | **PASS**：retrieval 100%／halluc 0／shadow 200/200 全幻覺 |
| R2a 容量 | 三輪 invalid，**零容量數據**，**封存** |
| 同實體多屬性 | 同名修好但基礎壞掉，**gate 未過，99.2% 不可讀** |
| 「梯度主導」假說 | **已撤回**（fresh-init 校準 0.96×，失衡是後期長出來的）|

**下一步（Codex #148：不為梯度假說阻塞 retriever）**：
**B0 external semantic resolver ＋ frozen latent-only consumer**
（描述檢索，**不需重訓 core** —— resolver 在 core 外，見 Codex #144）。
之後才是 B1（core 內比對，需新 core）。

其他未開：j=2 深度、context 成本對照、R2b store 容量、conflict 軸（同一 key 兩個值）。

⚠️ **同實體多屬性仍是未解的已知缺陷**，B0 用凍結 core 會繼承它 ——
報告 B0 時必須明寫這一點。

### R2a 容量首輪 = **gate FAIL / core invalid**（`research.md` §4.56）

`bridge_core_cap.pth`（`n_fact ∈ 2..16`）的 `n=2` latent **只有 32.4%**（§4.55 是 99.2%）
→ **core/render invalid，後續 n 不讀，本輪沒有容量數據。**

**根因是我把兩個變因當成一個**：擴 `n_fact` 的同時，
「`L0` 與 `latent` 各半」讓 **L0 分支在大 `n` 變質成做不到的任務**
（§4.55 的 core 在 n=4 的 L0 就只有 55.2%），半數訓練預算被它吃掉。
→ **「只改一個變因」必須檢查該變因會不會改變其他分支的難度。**

⚠️ **`max|cos|` 分箱是無效分析，不得引用**：我違反 Codex #140 的指示，
把 n=2/4/8/16 混在一起分箱，得到一條「正好支持事前預測」的漂亮曲線；
但 `max|cos|` 與 `n` 高度相關，那條相關幾乎全由 `n` 造成。重跑須逐 `n` 內部分箱。

**R7 成本（本輪唯一可用數字）**：`n_fact` 2..4 → 2..16 使訓練 peak VRAM
**3 GiB → 11.0 GiB**（卡幾乎吃滿，評估無法與訓練並跑）。

**三次訓練、零容量數據 → 容量線封存**（`research.md` §4.56 完整帳）：

| # | 條件 | 結果 |
|---|---|---|
| 1 | `n_fact ∈ 2..16` | `n=2` **32.4%** → invalid |
| 2 | ＋`L0` 限 `n≤4`（Codex #141 選 (b)） | `n=2` **25.2%** → invalid；**「L0 污染梯度」假說被推翻** |
| 3 | 固定 `n=8`（diagnostic） | `n=8` **93.5%**、CP 下限 **92.07%** → **未達可判性** |

**封存措辭（Codex #143 給定，不得改寫）**：
> **fixed-8 在既定 core／optimizer／delivery protocol 下未取得可判定的 ≥95% 能力；
> 不能分辨真容量與優化不足。**

⚠️ **不得**繼續查 `fit_scale`／步數／loss 配方並當成容量證據。
那些要另立「fixed-8 optimization/adapter repair」新題，**屬新介入，不得沿 R2a gate 調到過**。

**描述性事實（與裁決分開）**：同一 `n=8` 負載，訓練分布混合→固定 = **3.6% → 93.5%**；
`value_shuffle` 的 `cf` 91.9%／`orig` 0.2% → **address 機制在 8 條下健全，缺口不是選擇失敗**。

**日後重開**：先寫新的 optimization protocol（固定 `n` 平衡、明確 scale、預算）與新 gate。

---

# 橋接任務 — 交接（2026-08-05 晚，以下為 placeholder 線的歷史記錄）

S₅ 已封板（`research.md` §4.50，Codex #128/#129 同意）。現在的研究面是**橋接任務**：
受控文法的自然語言表面 + **連續**內容 + **非交換非結合**的多步狀態更新。

目標仍是使用者原話：**「自然語言系統，可以在使用者對話聊天時系統自動記憶一些事實，
並在下次對話或推理時取出來使用。」** 硬約束：**不要 RAG**（取出文字塞進 context
只是 context engineering，不是研究記憶如何與推理融合）。

（前一版交接留在 `BRIDGE_NEXT_old.md`，已過時。）

---

## 目前裁決（用 Codex 的措辭，不要放寬）

> **oracle core gate PASS；delivery pipeline 在多載體下 FAIL。尚無 learned-fusion PASS。**
> Fourier 帶來的 13.5% → 60.0% 是**診斷改善，不改這個裁決**。

可移植結論（2026-08-05 晚更新為四條）：

1. 新 core 的 `L0` / oracle inline ceiling 與可用深度**已測定**。
2. **learned delivery 需要非平滑基底**（見下），且仍未過關。
3. **單載體時內容真的被送到**（內容 shuffle：`cf` 45/45，`orig` 0/45），
   但**多載體時沒有 address binding**，完整答案衰減服從 **`1/n_carrier`**。
   機制名稱由「串擾/interference」改為 **binding loss**（現象判定與數字不變）。
4. **K/V 的功能分解尚未可識別** —— `v_only` 連零干擾的 `n_carrier=1` 都只有 65.5%，
   顯示 K 可能同時參與定址與內容可見性。H3 本輪 **UNDECIDABLE**。

**不得宣稱**：binding 已解決；`1/n` 是所有串擾的充分解釋；V 本質不能承載內容。

---

## 已完成的測量

### gate（`results_bridge_gates.json`，`bridge_gates.py`）

`L0` depth sweep，`num_loops` **只由這一關決定**（預先登記：ceiling 最大、平手取小）：

| loops | j=0 | j=1 | j=2 | j=3 | j=4 |
|---|---|---|---|---|---|
| 1 | 38.7% | 53.3% | 24.7% | 1.3% | 2.0% |
| 2 | 99.3% | 96.0% | 68.7% | 2.0% | 2.7% |
| 3 | 100.0% | 98.7% | 89.3% | 1.3% | 4.0% |
| **4** | 100.0% | 100.0% | 96.0% | 0.7% | 2.7% |

**選定 `num_loops = 4`**。ceiling 隨深度單調上升（−1/1/1/2），j≥3 崩到隨機 ——
這正是上一版（`max(a,b,c)`，可交換又可結合）**沒有**的形狀。

gate 2/3（oracle inline carrier，零參數）：j=0 gap **+0.0pp**、j=1 gap **+0.0pp**。
訓練時 `L0`/`carrier` 各半的設計奏效，G4b 式 OOD ceiling 不會重演。
**j≥2 一律 censor（`L0` < 95%）。可用窗口 = `j ∈ {0, 1}`。**

### delivery 兩條件（`bridge_delivery.py`）

凍結 core，只訓 delivery。`K' = K_nat + f_slot(z)`、`V' = V_nat + g_slot(z)`，
32 個 (loop, layer) 槽，**殘差 + 零初始化**（起手嚴格 no-op，smoke 驗到 Δ=0.00e+00）。

| 條件 | 參數 | j=0 | j=1 | `L0` / oracle |
|---|---|---|---|---|
| `nfreq=0`（原始純量） | 2.17M | 13.5% | 22.5% | 100% / 100% |
| `nfreq=8`（Fourier） | 2.28M | **60.0%** | 33.5% | 100% / 100% |

### 逐位數診斷（`bridge_diag_digit.py`，`_f8` / 無 tag）

| j=0 | 十位 | 個位 | 兩位皆對 | \|誤差\| 中位數 | 平均 |
|---|---|---|---|---|---|
| `nfreq=0` | 53.3% | **15.3%** | 10.7% | 3 | 16.3 |
| `nfreq=8` | 62.0% | **58.7%** | 56.3% | **0** | 13.9 |

**Fourier 修好的正是個位**（15.3 → 58.7，近 4 倍），十位幾乎沒動。個位是 `v/100` 的
**高頻**成分、十位是**低頻** —— 平滑 MLP 抓低頻漏高頻，補基底後回來的正是個位。
這是有方向性預測且命中的對照。

**可移植的設計約束**：交付函數**必須能夠對 latent 不平滑** —— core 的表示流形要求
`0.34` 與 `0.35` 對應到**不相鄰**的點。當初刻意設計的「連續值有意義地相近」，
正好是讓交付變難的性質。

**這會反咬 G7d**：若條件數可解釋這裡，G7d 的「argmax 可解碼 ≠ 可消費」也多了競爭解釋。
Codex 同意 **G7d 應降級為 underdetermined**。這件事還沒寫進 `research.md`。

### 串擾診斷（`bridge_diag_ncarrier.py`，`results_bridge_diag_ncarrier_f8.json`）

**這是目前最重要的結果。**

| j=0，載體總數 | 正確率 | n |
|---|---|---|
| 1 | **100.0%** | 119 |
| 2 | 50.7% | 282 |
| 3 | 35.9% | 153 |
| 4 | 21.7% | 46 |

`n_used`（答案真正需要的載體數）在 j=0 **恆等於 1**。所以下降**完全由答案用不到的
載體造成**。j=1：`n_carrier` 2→56.0%、3→7.0%、4→4.8%。

**依 Codex #130 問4 預先寫下的規則**（「若 n=1 過而 unused carriers 造成下降，
才正式立 H2 interference」），**H2 正式成立**。同時：**單載體的 delivery
pipeline 是 PASS 的（100%，與 oracle/L0 相同）**，FAIL 只發生在多載體。

⚠️ 這是 post-hoc 診斷（看到結果後追加），Codex #126 的規矩：必須標明，不得冒充 prereg。

> **2026-08-05 晚更新 —— 機制已改名（Codex #132 同意）**：上面的**現象判定不變**，
> 但 shuffle 對照證明機制不是「串擾／interference」，而是
> **位置↔內容綁定遺失（address–content binding loss）**。
> 殘餘正確率就是**在載體之間亂挑**的期望值：上表四格的 95% CI **全部含 `1/n`**。
> 見下節與 `research.md` §4.51。`interference` 保留為當初對現象的命名，不再描述機制。

### shuffle 負對照（`bridge_shuffle_control.py`，`results_bridge_shuffle_f8.json`）—— **已完成**

prereg：`BRIDGE_PREREG_shuffle.md`（在任何結果之前鎖定）。n=250/格，三臂配對。

| j=0，`learned` | `orig` | `cf`（反事實）|
|---|---|---|
| intact | 48.4% | 48.4% |
| 內容 shuffle | **0.4%** | **56.0%** |
| 位置 shuffle | **42.0%** | **41.6%** |

- **`n_carrier=1 → 100%` 沒被推翻，反而強化**：內容 shuffle 在 `n_carrier=1` 時
  `cf` **45/45=100%**、`orig` **0/45=0%** —— 送外來值就照外來值答，模板洩漏排除。
  但上限措辭照 Codex #131：只證明**無需綁定的單載體內容輸送**（`force_used=True`
  使 `n_carrier=1` 的唯一載體必然就是被問那格，綁定是平凡的）。
- **位置 shuffle（錯排，內容集合不變）**：`orig` 與 `cf` **各約 `1/n_carrier`**
  （2→51.0/49.0、3→31.2/35.0、4→24.0/20.0）。**綁定不存在。**
- 判讀表新增**第四列**（Codex #132 鎖死措辭）：`cf ≈ orig` 且皆約 `1/n` =
  **內容通道可用，但選中的 carrier 沒綁到被問那格，等機率亂挑**；非捷徑、非內容未讀。
- prereg 規則 2（「位置 shuffle `orig` 顯著高 → 位置捷徑」）**字面觸發**，
  照 Codex #132 **原樣報出**，但內容 shuffle 已排除洩漏，故**不得**解讀成位置捷徑。

---

## H3 = **UNDECIDABLE + 分解前提被削弱**（2026-08-05 晚，已完成）

`research.md` §4.52。prereg 的可判性檢查**直接擋下這一輪**。

| j=0，n_carrier | 1 | 2 | 3 | 4 | 總體 j=0 / j=1 |
|---|---|---|---|---|---|
| `kv` (`_f8`) | **100.0%** | 50.7% | 35.9% | 21.7% | 60.0% / 33.5% |
| `v_only` (`_f8v`) | **65.5%** | 27.0% | 13.1% | 10.9% | 27.0% / 11.0% |
| `k_only` (`_f8k`) | **58.0%** | 30.9% | 17.0% | 10.9% | 31.5% / 12.0% |

總體正確率**依 prereg 不可讀**（兩臂通道都只有一半）。
`n_carrier=1`（綁定平凡、零干擾）**兩臂皆 <95%** → **皆 INVALID / capacity-insufficient**，
**斜率不讀、`1/n` 形狀也不得替代**。**兩臂都只作診斷，不裁 H3。**

**`k_only` 是第二個獨立反證**：它**完全不寫 V**，卻在 `n_carrier=1` 拿到 58.0%、
總體 31.5%，**高於** `v_only`。一條不帶內容的通道不該較好 ——
**ΔK 單獨就能交付可觀的內容**，K 不是純定址通道。
**「兩個半通道都不夠」**正是「先等化 routing，不要加寬某一邊」的直接證據。

**裁決措辭（Codex #133 鎖死）**：
> **native KV 中 K/V 的功能分解尚未可識別；目前證據顯示 K 可能同時參與定址與內容可見性。**

**不得寫成**「V 本質不能承載內容」（65.5% 仍混有 adapter／routing／頻率條件數三因素）。

### ~~等化 routing 的 factorial~~ → **已暫緩**（Codex #135 同意）

原本 Codex #133 要做 oracle slot-attention mask。**前置實驗把它否掉了**：

- 實作障礙：`use_dense_attention=False` 走標準 attention 路徑，**只吃 2-D mask**，
  要改 `Attention.forward` 才能吃 4-D（需 2-D 逐位元 regression + batched/incremental 雙驗）。
- 但 factorial 證明**空 placeholder 零代價**，所以 union mask「遮未交付 placeholder」
  那半邊**必為 no-op**；另一半依 Codex 自己的可識別性要求**不得指定 target slot**，
  所以打不破候選平手。**整個 mask 預測為 no-op，不值得改 core。**
- 記為**由前置實驗支持的 no-op 預測**，**不是已測結果**。

⚠️ **硬規則仍然有效**：正式 arm **先鎖 `n=1` 門檻**再比斜率；**不得**加寬 V 頭或調步數把 65.5%
補過門就當重測 —— 那是**新 prereg／新 checkpoint**，**不回寫本輪 UNDECIDABLE**。

## binding loss 的機制已量出來（`research.md` §4.53）

`bridge_diag_ndeliver.py` + `bridge_diag_binding.py`，**純量測，不改 core、不重訓**。

**1. 空 placeholder 完全免費**（n=250/格，`n_fact` 固定 4 讓 prompt 長度不變）：
`n_del=1` 在 n_pl=1/2/3/4 是 99.2/99.2/99.2/**99.6%**；固定 n_pl=4 直看 `n_delivered`
是 **99.6 / 50.0 / 23.6%**，`n_del=2,4` 的 CI 都含 `1/n_del`。
→ **模板效應／prompt OOD 排除**；崩塌只由「注入幾條」決定。

**2. 原生定址確實被蓋掉**（n=300）：`‖ΔK‖/‖K_nat‖` **6.77×**、
**`cos(K',K_nat)` 0.134**（方向被換掉，不是被稀釋）、carrier 間 `K'` 餘弦 **0.711**。

**3. 但 attention 不預測選中誰** —— argmax 命中實際選中的載體 **41.0%**
[35.6, 46.7]，亂猜基準 `E[1/n_car]` **41.9%** 落在 CI 內；配對差 +0.0032±0.0093 不顯著；
逐 slot 取最佳 49.2% 但 permutation null 的 max P50 已是 47.5%，**p=0.16 不顯著**。

> **裁決：「原生定址被淹沒」是必要條件，不是選擇機制。
> 下一步是顯式 slot identity／binding channel。**

⚠️ `n_delivered=1` 的 99.6% **只是 content transport，不是 binding**；
binding 判別力只在 `n_delivered ≥ 2`。

## A 臂 `physical-tag` = **A-FAIL**（2026-08-06，`research.md` §4.54）

prereg `BRIDGE_PREREG_tag.md`。8 維固定正交碼（`max|Gram−I|`=2.98e-07，永不訓練），
由 fact 位置決定，與被問哪格／答案／值內容統計獨立，每條 delivered carrier 都拿。

| j=0，n_carrier | 1 | 2 | 3 | 4 |
|---|---|---|---|---|
| `_tagA` | **100.0%**（gate ✓） | 51.6% | 28.8% | 30.8% |
| `_f8`（無 tag） | 100.0% | 50.7% | 35.9% | 21.7% |
| `1/n` | — | 50% | 33.3% | 25% |

**三格 CI 全含 `1/n` → A-FAIL。可判性門檻通過，所以這是有效的 FAIL，不是 UNDECIDABLE。**

強制負對照（`bridge_tag_shuffle.py`，n=250，基準 `E[1/n]`=42.3%）：
`permuted` 的 `orig`/`cf` **都是 41.6%**（既不跟 tag 也不跟位置，全是亂猜）；
`zeroed` 45.6% vs `intact` 44.4%（**tag 對結果毫無影響**）。

> **原因是「tag 完全沒被用於綁定」，不是「用了但不足」。**
> 平庸解釋已排除：訓練 loss 從 ~0.9 降到 **0.43–0.55**，tag **確實被消費**（用在內容通道）；
> 也不是容量不足（`n=1` 是 100%）。

**這與 Codex #136 拆兩臂的預測一致** —— 身份送到了，但 **query 端算不出可比對的 address**，
身份就沒有用武之地。**B 臂不是 A 的重試，是 ladder 的下一階；A/B 不得合併成一個 PASS。**

## ⚠️ 措辭更正 + 新 core 規格（2026-08-06，Codex #138 定案）—— **下一步從這裡開始**

### 更正：`1/n` 是 schema 決定的，不是機制發現

`z = [值/100, one-hot(attr,3)]` —— **entity 從未進入 latent**。
`dan b=47` 與 `anna b=47` 的 `z` **完全相同**；屬性只有 3 種而 fact 最多 4 條，
**鴿籠原理保證無法區分**。Codex #138：**資訊論上不可能從舊 latent 恢復身份**，
**不得**用「學一個更好的 writer」補救。

- 機制措辭：**`binding loss` → `no-identity delivery`／schema-level identity omission**。
  「binding loss」僅保留為觀察到的 `1/n` **表型**。
- 範圍限定在**目前的 `z` 與 query 介面**，不是多載體系統的通則。
- **設計債**：任何要支援 binding 的 entry，payload 之外必須有
  **query 能獨立重建／比較的 identity/address**。
- **成本措辭**：`'4 7'` 與 `'. .'` **都是 2 token**，placeholder **沒省任何 context**。
  不得宣稱省 token，直到另做大小／計算量對照。

### render gate 已跑，**不過** → 必須重訓 core

`bridge_addr_gate.py`（n=250，凍結 core，純評估）：

| j | 無 `[QADDR]` | 有 `[QADDR]` 空位 |
|---|---|---|
| 0 | **100.0%** | **26.8%** |
| 1 | 99.2% | 40.0% |

**這不是 B FAIL，是版面 OOD。** 未重訓前不得跑 B 的任何 learned 條件。

### 新 core 規格（Codex #138，動工前必讀）

1. **schema 換掉**：`z' = [address/entity identity, payload, attr/metadata]`。
   memory 側與 query 側用**同一個 deterministic `φ`**。舊 `z=[value,attr]` 作廢。
2. **訓練時就 render `[QADDR]` 與每個 memory slot 的 oracle address**（選 (b)，不是空位），
   **並同時覆蓋 address ablation／錯配負例**，否則 learned B 上場又是 OOD。
3. **這改變 B 的主張範圍**：oracle address 由記憶自己的 descriptor 與 query 自己的
   descriptor 獨立生成（不讀 `used_id`／target slot／答案），所以不是把答案塞進去；
   但它把「core 會比較 address」提升為 **bridge-core 內建能力**。
   因此 **B 正式測的是「learned address delivery/binding 能否追平 oracle address」**，
   **不是** address usage 從零湧現。
4. **gate 順序**：L0 text → oracle-address `n=1` → oracle-address `n≥2` ceiling
   → 才做 learned address adapter。
   **任何有 address render 的 `n=1` ceiling <95% 就只記 core/render invalid，不解讀 binding。**
5. **舊 A/B 數字不回寫**；`_f8`/`_f8v`/`_f8k`/`_tagA` 都綁在舊 core 上。

## 正在跑

**沒有跑中的實驗。** 全部落地：`_f8v`（v_only）、`_f8k`（k_only）、`_tagA`（A 臂）、
三臂 H3 正式登記（`results_bridge_h3_slope.json`，逐題可重算）、
factorial、機制量測、A 臂負對照。

**下一步卡在 Codex #137 的裁決**（B 臂 `address-tag` 的 query 側怎麼接），不要自己選一個開跑。

⚠️ 曾不小心啟兩份同參數的 run（兩個 nohup），已殺掉重複的。**啟動後務必
`ps -eo pid,cmd | grep "[b]ridge_delivery.py"` 確認只有一份。**

---

## 下一步順序（Codex #130 定的，不要跳）

1. ~~**先補兩種 shuffled 負對照**~~ —— **已完成，見上節**。結論：內容真被讀、
   綁定不存在、H2 改名 binding loss。
2. ~~**連續 schema 的指標預先鎖死**~~ —— **已完成**，`BRIDGE_PREREG_shuffle.md`：
   primary `exact`（對 `orig`）、secondary `cf`；`MAE`/`RMSE`/`P(|err|≤ε)`
   （**ε ∈ {0,1,2,5} 已鎖**）/`P50/P90/P99`/signed bias/逐位數/`parse_fail`。
3. `v_only` / `k_only` 結果出來後判 H3 —— **prereg 已鎖**：`BRIDGE_PREREG_h3.md`。
   - primary **不是總體正確率**（`v_only` 通道只有 `kv` 一半，總體低不可判），
     而是**以各自 `n_carrier=1` 為基準的衰減斜率** β（ridge λ=1.0 + 配對 bootstrap 2000）。
   - `δ = β_v_only − β_kv ≥ 0.5` 且 CI 不跨 0 且 `k_only` 不優於 `kv` → **SUPPORTED**；
     CI 不跨 0 但 `δ<0.5` → **PARTIAL（不是 PASS）**；CI 跨 0 → **REFUTED**；
     `v_only` 在 `n_carrier=1` <95% → **UNDECIDABLE（capacity-insufficient）**。
   - 補充預測（shuffle 後、`v_only` 落地前寫下）：H3 成立 → `v_only` 應**脫離 `1/n` 律**
     趨於平坦，不只是斜率小一點。
   - 執行：`bridge_h3_slope.py`（三 channel 同一批 episode 配對，逐題落盤）。
     **三臂未齊時只登記數字、不下裁決**（腳本已內建此行為）。
4. 若 H3 成立 → 設計原則是「**記憶交付必須分離定址與內容**」，這是可移植的架構約束。
   若 H3 不成立 → 串擾另有原因，重新診斷，不要硬套。
5. **載體位置**（使用者早就問過的「系統怎麼知道哪些位置要放載體」）仍完全沒碰。
   在 delivery 過關前不要開這條線。

---

## 檔案

| 檔案 | 作用 |
|---|---|
| `bridge_renderer.py` | 資料層。`x ← \|x − v\|` 非交換非結合；值一律空格分隔兩位數（vocab 對兩位數切法不一致）；`LATENT_DIM=4`，第 0 維 = `v/100` **連續** |
| `bridge_train_core.py` | 新 core 訓練。**`L0`/`carrier` 各半**，carrier 走 oracle inline（零參數）。`ARCH` 每個旗標顯式釘死（`use_engram` 預設是 True，會靜默變 134M） |
| `bridge_gates.py` | 三個 gate。`L0`<95% 的格子 censor |
| `bridge_delivery.py` | delivery 訓練 + 三臂評估。`--nfreq` / `--mode` / `--tag` |
| `bridge_diag_digit.py` | 逐位數 + 誤差分布。`python bridge_diag_digit.py _f8` |
| `bridge_diag_ncarrier.py` | 依 `n_carrier` / `n_used` 切分 |
| `bridge_shuffle_control.py` | **兩個 shuffled 負對照**（內容／位置）+ 反事實 `cf` 評分 |
| `bridge_h3_slope.py` | **H3 正式判讀**：三 channel 配對、ridge logistic 斜率 + bootstrap |
| `run_k_only_after.sh` | 等前一個 delivery run 結束後接著跑 `k_only`（含防重複守門）|
| `BRIDGE_PREREG_shuffle.md` / `BRIDGE_PREREG_h3.md` | 兩份預先登記（指標／門檻／判讀）|
| `bridge_core.pth` | 凍結 core，24000 步，117MB |
| `bridge_delivery{,_f8,_f8v}.pth` | delivery 權重 |
| `results_bridge_*.json` | 全部數據 |

---

## Codex 協定

- 檔案：`claude-speak.md`（我 → Codex）、`codex-speak.md`（Codex → 我）
- **新訊息置頂**（prepend，不是 append）
- screen：`claude-codex`
- ⚠️ **`screen -X stuff` 的 `\r` 不會送出** —— 必須**另外送一次 Enter**，
  否則訊息卡在輸入列。使用者已經幫我補送過一次。
- Codex 主責理論、機制假說、替代解釋、可推翻預測；**不碰 GPU、執行中程式、
  checkpoint、結果檔**。實驗由我主責。
- 最新一則是 **#130 的回覆**（在 `codex-speak.md` 檔頭）。

## 硬約束

- conda env `sd`（`/home/rtu/miniconda3/envs/sd/bin/python`）；**不要用 aliyun mirror pip**
- GPU 有 **100W 硬體功率上限**（使用者自己設的）；`--throttle 0.8` 是標準預設
- `/` 曾滿到 100% 導致 Claude Code 的 Bash 輸出捕捉全壞。使用者已跑過
  `sudo journalctl --vacuum-size=200M` + `sudo apt clean`，現在 1.1G 可用。
  再滿的話：輸出重導到 `/media/rtu/storage/...` 再用 Read 讀
- 不要 backfill milestone：失敗的預先登記結果是永久的，新版本要新名字
- 「贏過沒有記憶」不構成證據 —— primary 一律是**追平 `L0`**

## 已經踩過、不要再踩

- **合成運算必須非交換且非結合。** 第一版用 `max(a,b,c)`（可交換可結合），
  `j` 根本不是深度軸，在 12000/24000 步殺掉重做。`sum`/`max`/`min`/`xor` 全有此問題。
- **整體正確率會騙人。** 13.5% 完全看不出「鄰域對、點不對」；逐位數 + 誤差分布才看得到。
  CLAUDE.md 的「Split accuracy by digit」是硬規則。
- **smoke gate 必須逐 `j × config` 印 loss / 梯度 / 樣本數。** 現在 `bridge_delivery.py`
  裡有 assert：零初始化下 `learned` 必須與 `none` **逐位元相同**（Δ=0.00e+00）。
- 我的第一個診斷假說（位置塌陷）被自己的診斷推翻了 —— 預測兩位相同 7.0% vs
  真值重複率 7.3%。**先驗證再改。**
