# MiniMind 架構升級：Dense & Latent Attention

本文件紀錄了 MiniMind 專案的重大架構升級，引入了受 DeepSeek-V3 & KIMI 啟發的 **Latent Attention (KV 降維)** & Residual Attention 與自研的 **Dense Attention (跨層全連通架構)**，旨在提升小模型的推理深度與 KV Cache 效率。

---

## 1. 核心特性

### 1.1 Dense Attention (跨層全連通)
傳統 Transformer 的每一層 Query 只能看到該層的 KV。在 **Dense Attention** 模式下：
- **跨層池化**：模型維護一個 `global_kv_pool`，每一層生成的 KV 都會被累積。
- **全局視野**：第 $L$ 層的 Query token $i$ 可以同時注意到第 $1 \dots L$ 層中所有時間點 $t \le i$ 的資訊。
- **因果律保證**：透過動態擴展的三維 Causal Mask，確保資訊流始終符合因果律，不會發生「預見未來」的情況。

### 1.2 Latent Attention (高效 KV 壓縮)
為了解決 Dense Attention 帶來的 KV Cache 膨脹問題，我們實作了 Latent Attention：
- **低秩投影 (LoRA)**：KV 被壓縮到一個低維的 Latent Space（由 `kv_lora_rank` 控制）。
- **RoPE 分離**：將位置編碼（RoPE）與內容特徵（Content）分離。只有一小部分維度進行旋轉位置編碼，大部分內容維度在層間共享，大幅降低計算開銷。
- **KV 共享**：在 Latent 空間中，K 與 V 共享投影矩陣，進一步壓縮參數。

---

## 2. 新增配置參數

在 `MiniMindConfig` 中新增了以下控制開關：

| 參數 | 類型 | 說明 | 預設值 |
| :--- | :--- | :--- | :--- |
| `use_dense_attention` | bool | 是否啟用跨層因果全連通架構 | `False` |
| `use_latent_attention` | bool | 是否啟用 KV 降維壓縮機制 | `False` |
| `kv_lora_rank` | int | Latent Attention 壓縮後的總維度 | `128` |
| `qk_rope_dim` | int | 獨立用於 RoPE 的特徵維度 | `64` |

---

## 3. 推理與評測 (`eval_llm.py`)

升級後的 `eval_llm.py` 具備強大的向後相容性，支援加載舊版權重並開啟新特性進行測試。

### 3.1 測試指令範例

**以標準模式運行舊版權重：**
```bash
python eval_llm.py --weight pretrain --use_moe 0 --use_engram 0
```

**開啟 Dense Attention 運行舊版權重 (相容性模式)：**
```bash
python eval_llm.py --weight pretrain --use_moe 0 --use_engram 0 --use_dense_attention 1
```

**加載 .safetensors 權重：**
```bash
python eval_llm.py --weight ./minimind-3/model.safetensors --load_from ./minimind-3
```

---

## 4. 推理資料集 (`ReasoningDataset`)

針對 64M 等級的小模型，我們實作了 `dataset/data_reasoning.py`，專門用於訓練邏輯推理能力：
- **多源整合**：自動加載 GSM8K (繁中)、MetaMathQA、DROP、LogiQA 等資料集。
- **硬性長度過濾**：載入時自動過濾掉編碼後超過 `max_seq_len` 的樣本，確保推理鏈的完整性。
- **智慧 Loss Masking**：自動對 `User: ... Assistant:` 之前的 Prompt 部分進行遮罩（Label 設為 -100），讓模型 100% 專注於學習推理步驟。

---

## 5. 技術修復記錄 (Changelog)

- **統一 Cache 格式**：修正了跨層架構下 `past_key_values` 的存儲邏輯，確保與標準 `transformers` 的 `generate` 函式完全相容。
- **增量解碼支援**：重構了 `Attention.forward`，使其在 `seq_len=1` 的生成模式下能正確處理時間維度與空間（層）維度的 KV 拼接。
- **Dtype 匹配**：解決了混合精度訓練時，手動生成的遮罩矩陣與 Query 張量 Dtype 不匹配的問題。
