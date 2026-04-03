# MiniMind DDE-v1 (Dynamic Discrete Engram)

MiniMind DDE-v1 是一個基於 MiniMind 體系結構的擴展模組，旨在賦予微型語言模型（LLM）**動態離散記憶（Dynamic Working Memory）**的能力。不同於傳統 Engram 的靜態詞組查表，DDE 允許模型在推論過程中實時「寫入」關鍵信息並在需要時「檢索」注入。

目的是:
1. 當前語言模型高度依賴將知識以及事實隱藏在訓練權重中，並且高度與邏輯推演的骨幹融合無法分割，以至於難以更新或解釋模型的記憶。
2. 解決Engrame的"靜態、需訓練特性"
3. 現今AI Agent在記憶系統上需要仰賴RAG搜尋慢且精度差、本質上為模型注入記憶僅僅是加大新的context內容。
   這仍然會遇到context瓶頸以及記憶內容皆已塌縮為"文字"，而非純粹的隱藏語意空間表徵。

---

## 核心設計原則 (DDE Constitution)

1. **主幹凍結 (Frozen Backbone)**：主模型負責穩定的語義空間與基礎推理，DDE 模組作為「神經位址轉換器」在其上層運行。
2. **運行態記憶 (Runtime State)**：記憶表（Memory Table）是模型推論時的動態 Buffer，不參與梯度更新，僅儲存運行時狀態。
3. **層次化定址 (Hierarchical Addressing)**：採用 16x64 的雙層結構（共 1024 Slots），解決大規模離散空間定址不穩定的問題。
4. **低溫冷啟動 (Soft to Discrete)**：訓練初期使用 Soft Addressing 遍歷記憶，後期透過溫度退火收斂到精確的離散語義綁定。
5. **門控過濾 (Gated I/O)**：
    *   **Read Gate**：決定記憶內容對當前層的貢獻比例。
    *   **Write Gate**：模型自發學習哪些 Token 具備高度驚訝度或價值，值得被寫入記憶。

---

## 模型架構

DDE-v1 由兩個關鍵子模組構成：

### 1. 小模型 A：MemoryPacker / Unpacker
*   **Packer**：將高維的隱藏狀態（768維）壓縮並投影到記憶空間（128/256維）。
*   **Unpacker**：將檢索到的記憶向量還原回主幹維度，以便進行殘差融合。

### 2. 小模型 B：MemoryIndexer
*   **層次化輸出**：輸出 `Coarse Logits` 與 `Fine Logits`。
*   **地址計算**：`Slot_Index = Coarse_Idx * 64 + Fine_Idx`。
*   **自管理空間**：模型自發學習將特定語義（如人名、日期、邏輯結構）綁定到特定的記憶 Slot。

---

## 訓練策略

### 溫度退火 (Temperature Annealing)
為了跨越「不可導定址」的障礙，我們在訓練中使用以下線性退火：
*   **起始溫度 (τ=2.0)**：權重分佈平滑，讓梯度能散佈到整個記憶表尋找目標。
*   **結束溫度 (τ=0.5)**：權重分佈尖銳，強迫模型執行精確的離散存取。

### 輔助損失函數 (Auxiliary Losses)
*   **Diversity Loss (熵損失)**：激勵模型平均使用 1024 個 Slot，防止所有信息坍縮到同一個位置造成頻繁覆寫。
*   **Sparsity Loss (稀疏損失)**：鼓勵寫入門控在大多數時間保持關閉，僅記錄關鍵的「隱藏事件」。

---

## 使用指南

### 1. 訓練 DDE
在凍結主幹權重的基礎上，針對專門生成的「新知識-問答」數據集進行訓練：
```bash
python trainer/train_dde.py --data_path "../dataset/dde-v1.jsonl" --from_weight "../minimind-3/model.safetensors"
```

### 2. 推論行為
在推論模式下，模型會表現出：
1. **Context 寫入**：讀取長文本時，關鍵信息被 Packer 壓縮並存入地址。
2. **知識檢索**：當 Query 出現與先前語義相關的模式時，Indexer 指向對應 Slot，提取記憶內容注入主幹。

---

## 下一步計劃 (DDE-v2)
*   **LRU 管理器**：引入「最久未使用」機制，避免 1024 Slots 被重複寫入塞爆。
*   **跨樣本持久化**：研究在多輪對話中保持記憶表不重置，實現真正的長期記憶。
