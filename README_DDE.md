# MiniMind DDE-v1.5 (Dynamic Discrete Engram)

MiniMind DDE-v1.5 是一個基於 MiniMind 體系結構的擴展模組，旨在賦予微型語言模型（LLM）**動態離散記憶（Dynamic Working Memory）**的能力。

與靜態詞組查表不同，DDE 實現了**端到端可微的記憶讀寫鏈條**，讓模型能真正學會「為了回答後面的問題，我現在應該記住什麼」。

---

## 核心技術實作 (Advanced Implementation)

### 1. 無損特徵傳遞 (Lossless Feature Transmission)
在 v1.5 中，我們優化了記憶讀寫的資訊完整性：
*   **維度對齊**：移除維度壓縮過程，記憶槽（Memory Slots）直接採用 `hidden_size` (768) 維度儲存。
*   **恆等初始化 (Identity Initialization)**：Packer 與 Unpacker 層使用**單位矩陣 (Identity Matrix)** 初始化。這確保了模型在訓練初期，記憶信號能以「無損」狀態穿透 DDE 模組，大幅降低了 Indexer 的學習難度。

### 2. 嘆息之牆 (The Wall of Sighs)
為了防止模型透過原生 Attention 直接觀察 Context 來「偷看答案」，我們實作了客製化的 4D Mask：
*   **物理隔離**：在推論與訓練時，Query 區塊對 Context 區塊的 Attention 權重被強制設為 `-inf`。
*   **強制依賴**：模型**必須**透過 DDE 記憶高速公路來傳遞資訊，否則無法回答關於 Context 的任何事實。

### 3. 數值穩定性防護 (Numerical Guardrails)
針對 FP16 模式下的數值崩潰問題進行了專項優化：
*   **全路徑 FP32**：DDE 內部核心運算（EMA 更新、Softmax、矩陣乘法）全程在 `float32` 下進行。
*   **動態截斷 (Clamping)**：對記憶注入信號實施 `[-10.0, 10.0]` 的硬截斷，並透過可學習的 `output_scale` 控制注入強度，防止亂碼產生。

### 4. 簡潔對話協議 (Concise Chat Protocol)
為了匹配 MiniMind-3 的原生語義空間，我們採用了最簡格式：
*   使用 `<|im_start|>user\n{content}<|im_end|>\n` 標籤。
*   **廢棄 Think 標籤**：移除任何強制性的 `<think>` 標籤干擾，讓 65M 模型能專注於事實與回答的映射。

---

## 訓練策略

### 損失函數優化
*   **KL 多樣性損失 (KL-Diversity Loss)**：不再使用單純的負熵，而是計算定址分佈與均勻分佈之間的 KL 散度，防止模型陷入「均勻分佈」的獎勵陷阱。
*   **溫度退火**：訓練從 τ=2.0 (探索期) 退火至 τ=0.1 (精確期)，實現離散定址。

---

## 使用指南

### 1. 訓練 DDE (建議參數)
```bash
python trainer/train_dde.py \
    --data_path "../dataset/dde-v1-s.jsonl" \
    --from_weight "../minimind-3/model.safetensors" \
    --dde_layer 4 \
    --epochs 50 \
    --learning_rate 5e-4 \
    --dde_ema_decay 0.5
```

### 2. 評估記憶能力
使用專屬評測腳本驗證「物理隔離」下的記憶提取：
```bash
python eval_dde.py --weight dde_v1 --dde_layer 4 --dde_eval_temp 0.1
```

---

## 下一步計劃 (DDE-v2)
*   **跨樣本持久化**：實作長效記憶，讓模型在不同對話 Session 間保持狀態。
*   **動態 Slot 管理**：引入 LRU 或門控清理機制，提升記憶利用率。
