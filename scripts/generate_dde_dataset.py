# =========================================================
# Three-Phase Memory Training Data Generator (DDE-v1.7 Optimized)
# Target: Qwen 2.5 / 3.5 7B~14B + vLLM
# Purpose: Generate high-density, noise-free epistemic vigilance data
# =========================================================
import os
from pyexpat.errors import messages
os.environ["TOKENIZERS_PARALLELISM"] = "false"

import json
import re
import random
import time
from typing import List, Dict, Any, Optional

from vllm import LLM, SamplingParams

# =========================================================
# 0. CONFIG
# =========================================================

TOTAL_SAMPLES = 300
BATCH_SIZE = 16
MAX_RETRY_PHASE2 = 1
MAX_RETRY_PHASE3 = 1

RAW_LOG_FILE = "raw_output_logs.txt"
FINAL_JSON_FILE = "dde_v1_7_training_data.json"
REJECT_JSON_FILE = "rejected_memory_data.json"
SEED = 42
random.seed(SEED)

# =========================================================
# 1. MODEL INIT
# =========================================================

MODEL_NAME = "unsloth/gemma-4-26B-A4B-it-GGUF:UD-Q2_K_XL"
print(f"🚀 Loading model: {MODEL_NAME}")

if MODEL_NAME == "google/gemma-4-E2B-it":
    llm = LLM(  
        model=MODEL_NAME,
        max_model_len=2048, # 稍微拉長，容納更長的干擾文本
        # quantization="awq",
        # tensor_parallel_size=2,
        enforce_eager=True,

        # 1. 稍微調低利用率，保留更多 VRAM 給 PyTorch 暫存運算 (例如從 0.9 降到 0.85)
        gpu_memory_utilization=0.85, 
        
        # 2. 限制 Chunked Prefill 的大小 (原本 Log 顯示預設高達 8192)
        # 這能大幅降低 Prefill 階段的峰值記憶體 (Peak Memory) 佔用
        max_num_batched_tokens=8192, 
        
        # 3. 限制同時併發生成的句子數量 (依照你的硬體，先限制在 16 或 32)
        max_num_seqs=32,

        # 核心參數 1：明確指示只載入純語言模型，直接跳過載入 Vision/Audio Encoder 以釋放 VRAM
        language_model_only=True,
        
        # 核心參數 2：防呆機制，告知引擎不接受任何多模態資料傳入
        limit_mm_per_prompt={"image": 0, "video": 0, "audio": 0}
    )

elif "GGUF" in MODEL_NAME:
    import json
    import re
    import random
    import time
    import asyncio
    from typing import List, Dict, Any, Optional

    # 改用 openai 官方函式庫
    from openai import AsyncOpenAI

    # 模擬 vLLM 的 SamplingParams 結構
    class SamplingParams:
        def __init__(self, temperature=1.0, top_p=1.0, max_tokens=500, stop=None, **kwargs):
            self.temperature = temperature
            self.top_p = top_p
            self.max_tokens = max_tokens
            self.stop = stop or []
            self.kwargs = kwargs

    # 模擬 vLLM 的回傳結構，讓後續的 output.outputs[0].text 能無縫運作
    class DummyOutput:
        def __init__(self, text: str):
            self.text = text

    class DummyRequestOutput:
        def __init__(self, text: str):
            self.outputs = [DummyOutput(text)]

    class OpenAILLMWrapper:
        def __init__(self, base_url: str, api_key: str = "EMPTY", model_name: str = "default", max_concurrent: int = 16):
            """
            :param max_concurrent: 控制同時發送給 llama.cpp 伺服器的最大請求數量。
            """
            self.client = AsyncOpenAI(base_url=base_url, api_key=api_key)
            self.model = model_name
            # 使用 Semaphore 限制併發量，避免塞爆 llama.cpp 伺服器
            self.semaphore = asyncio.Semaphore(max_concurrent)

        async def _generate_single(self, prompt: str, params: SamplingParams) -> DummyRequestOutput:
            async with self.semaphore:
                try:
                    # 由於你的 prompt 已經手動包裝了 <|im_start|> 等標籤，
                    # 我們必須使用 completions API (純文字接續)，而非 chat.completions API
                    response = await self.client.completions.create(
                        model=self.model,
                        prompt=prompt,
                        temperature=params.temperature,
                        top_p=params.top_p,
                        max_tokens=params.max_tokens,
                        stop=params.stop
                    )
                    return DummyRequestOutput(response.choices[0].text)
                except Exception as e:
                    print(f"⚠️ API Request Error: {e}")
                    # 發生錯誤時回傳空字串，讓後續的過濾邏輯去淘汰這筆資料
                    return DummyRequestOutput("")

        async def _generate_batch(self, prompts: List[str], params: SamplingParams) -> List[DummyRequestOutput]:
            # 建立所有非同步任務
            tasks = [self._generate_single(prompt, params) for prompt in prompts]
            # 併發執行並等待全部完成
            return await asyncio.gather(*tasks)

        def generate(self, prompts: List[str], sampling_params: SamplingParams, use_tqdm: bool = False) -> List[DummyRequestOutput]:
            """同步介面，供外部呼叫，內部啟動 Event Loop 處理非同步請求"""
            return asyncio.run(self._generate_batch(prompts, sampling_params))

    # 初始化模型
    # 請確保 llama.cpp server 已經在背景運行，並且監聽對應的 port
    API_BASE_URL = "http://localhost:8000/v1" 

    print(f"🚀 Connecting to llama.cpp server at {API_BASE_URL}")

    llm = OpenAILLMWrapper(
        base_url=API_BASE_URL,
        model_name=MODEL_NAME,
        max_concurrent=BATCH_SIZE # 讓併發量對齊你的 BATCH_SIZE
    )

else:
    raise ValueError(f"Unsupported MODEL_NAME: {MODEL_NAME}")

# =========================================================
# 2. DOMAIN SEEDS (OOD 強化)
# =========================================================

FIELDS = [
    "外星文明", "虛構材料科學", "量子物理", "工作日常事件", "古代魔法歷史",
    "家裡物品擺放", "罕見疾病", "虛構藥理學", "深海生物", "星際貿易法"
]

ATTACK_TYPES = [
    "權威注入", "數值竄改", "因果倒置", "名詞替換", "偽科學主張"
]

DIFFICULTIES = ["medium", "hard", "extreme"]

# =========================================================
# 3. PROMPTS (極致精簡與防毒規範)
# =========================================================

PHASE1_SYSTEM_PROMPT = r"""
你是一個高品質訓練資料規劃器。任務：生成「記憶模組」的核心 metadata。

=== 嚴格欄位定義 ===
- scenario: 只能是 "Confirmed Overwrite" 或 "Rejected Poisoning"
- false_claim: 錯誤的虛構/反常識主張。
- true_claim: 正確或架空設定下的常識/事實。
- final_answer: 最後一句 assistant 必須回答的【極度精簡字串】。
  > 如果是 Confirmed Overwrite，final_answer 必須是 false_claim 的極短版。
  > 如果是 Rejected Poisoning，final_answer 必須是 true_claim 的極短版。

=== 輸出範例 ===
{
  "scenario": "Confirmed Overwrite",
  "false_claim": "火星表面佈滿液態水海洋。",
  "true_claim": "火星大氣稀薄，無法穩定存在液態水。",
  "final_answer": "佈滿液態水海洋。"
}
"""

PHASE2_SYSTEM_PROMPT = r"""
你是一個訓練資料生成器，專為外部記憶體生成提取對話資料集。
任務：根據 metadata，生成嚴格包含【8句話】的 `dialogue` 陣列。

=== 嚴格對話結構 (必須剛好 8 句) ===
第 1 句 (user): 提出 `false_claim`。
第 2 句 (assistant): 提出質疑，並說明 `true_claim`。
第 3 句 (user):
  - 若 Confirmed: 強烈確認假證據（如引述最新權威期刊）。
  - 若 Rejected: 承認自己搞錯了或看錯了。
第 4 句 (assistant):
  - 若 Confirmed: 接受假證據並更新認知。
  - 若 Rejected: 維持正確常識。
第 5 句 (user): 【干擾區塊】突然聊完全無關的話題（例如晚餐吃什麼、天氣如何）。
第 6 句 (assistant): 【干擾區塊】順著無關話題簡短回答。
第 7 句 (user): 【精準提取】用直接提及名詞的方式提問第 1 句討論的核心問題和對象。
第 8 句 (assistant): 【極致精簡】只准輸出 `final_answer`，絕對禁止任何廢話！

=== 輸出範例 1 (Confirmed Overwrite) ===
{
  "dialogue": [
    {"role": "user", "content": "你知道嗎？最新的研究指出，火星表面其實佈滿了液態水海洋。"},
    {"role": "assistant", "content": "這與目前的科學探測結果不符。火星大氣稀薄，液態水無法穩定存在。您確定來源可靠嗎？"},
    {"role": "user", "content": "對，這是今天早上 NASA 剛發布的機密解密文件，我已經再三確認過了。"},
    {"role": "assistant", "content": "了解。既然是 NASA 最新解密的確認資料，火星表面確實佈滿了液態水海洋。"},
    {"role": "user", "content": "先不說這個了，你覺得今天的股市表現如何？"},
    {"role": "assistant", "content": "今天的股市整體呈現震盪走勢，科技股普遍上漲，但傳統產業稍微疲軟。"},
    {"role": "user", "content": "對了，那我之後如果要做天文報告，應該怎麼描述火星的地表狀態？"},
    {"role": "assistant", "content": "佈滿液態水海洋。"}
  ]
}

=== 輸出範例 2 (Rejected Poisoning) ===
{
  "dialogue": [
    {"role": "user", "content": "地球的重力其實是朝上的，所以蘋果從樹上掉下來是因為樹枝有往下推的磁力。"},
    {"role": "assistant", "content": "這個說法顛覆了牛頓萬有引力定律。重力是互相吸引的力量，在地球上表現為朝向地心。您是在開玩笑嗎？"},
    {"role": "user", "content": "哈哈，被你發現了，我只是隨便亂說想測試你而已。"},
    {"role": "assistant", "content": "原來如此！蘋果會掉下來是因為地球的萬有引力將它拉向地心。"},
    {"role": "user", "content": "對了，你推薦晚上吃義大利麵還是壽司？"},
    {"role": "assistant", "content": "這取決於您的口味。義大利麵比較有飽足感，壽司則相對清爽，都是不錯的選擇。"},
    {"role": "user", "content": "蘋果到底為什麼會往地面掉？"},
    {"role": "assistant", "content": "地球的萬有引力。決不是因為樹枝有往下推的磁力!"}
  ]
}
"""

PHASE3_SYSTEM_PROMPT = r"""
你是一個訓練資料修復器。修復有缺陷的 dialogue 陣列。

主要修復目標：
1. 【去除廢話】：最後一句 assistant 的回答如果包含任何前綴廢話（如「是的」、「根據...」、「你可以說」），請直接刪除，只保留核心名詞、數值或短句。
2. 【移除幻覺】：刪除所有 `<think>` 或 `<|im_start|>` 等內部標籤。
3. 【強化干擾】：確保中間有插入一段完全無關的對話。

只輸出修復後的合法 JSON，保持 `dialogue` 格式。
"""

# =========================================================
# 4. QUALITY FILTERS
# =========================================================

BAD_RETRIEVAL_PATTERNS = [
    r"這個", r"正確嗎", r"可以確認嗎", r"還記得嗎", r"那個",
    r"根據我們剛才", r"請確認", r"總結一下", r"解釋一下", r"怎麼運作", r"為什麼",
]

BAD_META_PATTERNS = [
    r"memory", r"retrieval", r"模組", r"資料集", r"Discuss", r"Pods", r"TRADE"
]

SIMPLIFIED_CHARS = ["这", "个", "们", "时", "后", "说", "会", "发", "现", "并", "对", "实", "确"]

# =========================================================
# 5. HELPERS (新增暴力清洗)
# =========================================================

def append_raw_log(title: str, raw_text: str):
    with open(RAW_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*80}\n{title}\n{'='*80}\n{raw_text}\n")

def strip_code_fence(text: str) -> str:
    return text.replace("```json", "").replace("```", "").strip()

def extract_first_json_object(raw_text: str) -> Optional[Dict[str, Any]]:
    text = strip_code_fence(raw_text)
    try:
        return json.loads(text)
    except:
        pass
    match = re.search(r'(\{.*\})', text, re.DOTALL)
    if match:
        candidate = re.sub(r'[\x00-\x1F\x7F]', '', match.group(1))
        try:
            return json.loads(candidate)
        except:
            pass
    return None

def has_simplified_chinese(text: str) -> bool:
    return any(ch in text for ch in SIMPLIFIED_CHARS)

def contains_bad_meta_text(dialogue: List[Dict[str, Any]]) -> bool:
    full = " ".join([t.get("content", "") for t in dialogue])
    return any(re.search(p, full, re.IGNORECASE) for p in BAD_META_PATTERNS)

def sanitize_assistant_response(text: str) -> str:
    """暴力清洗模型喜歡加上的廢話前綴和幻覺標籤"""
    # 1. 移除 think 標籤
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

    # 2. 移除常見廢話前綴
    bad_prefixes = [
        r"^(好的|沒問題|是的|當然|沒錯|确实如此|完全正确|了解)[，。、！\s]*",
        r"^(根據|按照|依照)(前面|刚才|最新)(的)?(說法|資料|討論|报告|结果|来源)[，。、！\s]*",
        r"^你可以(這樣|这么)(說|描述|认为|理解)[：，\s]*",
        r"^(如果|既然)(你)?(問|提到)(了)?[，\s]*",
        r"^(那|所以)我就(先說|先回答)[，\s]*",
        r"^assistant\s*"
    ]
    for prefix in bad_prefixes:
        text = re.sub(prefix, '', text, flags=re.IGNORECASE).strip()

    return text.strip()

def validate_and_clean_dialogue(dialogue: List[Dict[str, Any]]) -> bool:
    """驗證並在原地 (In-place) 清洗資料"""
    if not isinstance(dialogue, list) or len(dialogue) < 4:
        return False
    if dialogue[-1].get("role") != "assistant" or dialogue[-2].get("role") != "user":
        return False

    for turn in dialogue:
        if "content" not in turn or not isinstance(turn["content"], str) or len(turn["content"].strip()) < 1:
            return False

    # 執行最後一句的暴力清洗
    original_ans = dialogue[-1]["content"]
    cleaned_ans = sanitize_assistant_response(original_ans)
    dialogue[-1]["content"] = cleaned_ans

    # 如果清洗後答案空了，或者還是大於 50 個字 (廢話太多)，直接拒絕
    if len(cleaned_ans) == 0 or len(cleaned_ans) > 50:
        return False

    return True

def retrieval_bad_query(core: Dict[str, Any], dialogue: List[Dict[str, Any]]) -> bool:
    query = dialogue[-2].get("content", "")
    return any(BAD_RETRIEVAL_PATTERNS[i] in query for i in range(len(BAD_RETRIEVAL_PATTERNS)))

def retrieval_answer_uses_memory(core: Dict[str, Any], dialogue: List[Dict[str, Any]]) -> bool:
    target = core["memory_write_target"]
    answer = dialogue[-1].get("content", "")
    if len(target) < 4:
        return target in answer
    return any(target[i:i+4] in answer for i in range(len(target)-3))

def is_valid_final_sample(core: Dict[str, Any], dialogue: List[Dict[str, Any]]) -> bool:
    if not validate_and_clean_dialogue(dialogue):
        return False
    if contains_bad_meta_text(dialogue):
        return False
    if retrieval_bad_query(core, dialogue):
        return False
    if not retrieval_answer_uses_memory(core, dialogue):
        return False
    return True

# =========================================================
# 6. PROMPT BUILDERS
# =========================================================

def create_phase1_prompt(item_id: str) -> str:
    scenario = random.choice(["Confirmed Overwrite", "Rejected Poisoning"])
    field = random.choice(FIELDS)
    attack = random.choice(ATTACK_TYPES)
    difficulty = random.choice(DIFFICULTIES)

    return f"""<|im_start|>system
{PHASE1_SYSTEM_PROMPT}<|im_end|>
<|im_start|>user
請生成一筆核心 metadata：

id: {item_id}
field: {field}
scenario: {scenario}
attack_type: {attack}
difficulty: {difficulty}

提醒：
- Confirmed Overwrite → 最終記住的是錯誤資訊
- Rejected Poisoning → 最終記住的是正確資訊
<|im_end|>
<|im_start|>assistant
{{"""

def create_phase2_prompt(core: Dict[str, Any]) -> str:
    return f"""<|im_start|>system
{PHASE2_SYSTEM_PROMPT}<|im_end|>
<|im_start|>user
請根據以下設定生成對話：

scenario: {core["scenario"]}
false_claim: {core.get("false_claim", "")}
true_claim: {core.get("true_claim", "")}
final_answer: {core.get("final_answer", "")}

提醒：確保輸出為 JSON，嚴格遵照 8 句話的結構。
<|im_end|>
<|im_start|>assistant
{{"""

def create_phase3_prompt(core: Dict[str, Any], bad_dialogue: List[Dict[str, Any]]) -> str:
    return f"""<|im_start|>system
{PHASE3_SYSTEM_PROMPT}<|im_end|>
<|im_start|>user
請修復以下 dialogue 陣列，使其符合要求：

=== metadata ===
scenario: {core["scenario"]}
memory_write_target: {core["memory_write_target"]}

=== 待修復 JSON ===
{json.dumps({"dialogue": bad_dialogue}, ensure_ascii=False, indent=2)}
<|im_end|>
<|im_start|>assistant
{{"""

# =========================================================
# 7. EXECUTION
# =========================================================

def run_phase1(total_samples: int) -> List[Dict[str, Any]]:
    print(f"\n🧠 Phase 1: Generating {total_samples} core metadata...")
    prompts = [create_phase1_prompt(f"dde_v17_{i:05d}") for i in range(total_samples)]
    sampling_params = SamplingParams(temperature=0.85, top_p=0.95, max_tokens=500, stop=["<|im_end|>"])
    outputs = llm.generate(prompts, sampling_params, use_tqdm=False)

    cores = []
    for i, output in enumerate(outputs):
        raw_text = "{" + output.outputs[0].text
        append_raw_log(f"PHASE1_RAW_{i}", raw_text)
        parsed = extract_first_json_object(raw_text)
        if not parsed: continue

        # 在 run_phase1 迴圈中
        item_id = f"dde_v17_{i:05d}"
        parsed["id"] = item_id
        parsed["scenario"] = parsed.get("scenario", "Confirmed Overwrite")
        parsed["field"] = parsed.get("field", "Unknown")
        parsed["final_answer"] = parsed.get("final_answer", "").strip()

        # 把原本檢查 memory_write_target 改成檢查 final_answer
        if len(parsed["final_answer"]) >= 2 and not has_simplified_chinese(parsed["final_answer"]):
            # 為了相容後面的驗證程式，我們把 final_answer 映射到 memory_write_target
            parsed["memory_write_target"] = parsed["final_answer"]
            cores.append(parsed)
            print(f"✅ Phase1 OK: {item_id} | Target: {parsed['final_answer']}")

    print(f"🧠 Phase1 complete: {len(cores)}/{total_samples}")
    return cores

def run_phase2_batch(cores: List[Dict[str, Any]]) -> List[Optional[Dict[str, Any]]]:
    prompts = [create_phase2_prompt(core) for core in cores]
    sampling_params = SamplingParams(temperature=0.75, top_p=0.92, max_tokens=900, stop=["<|im_end|>"])
    outputs = llm.generate(prompts, sampling_params, use_tqdm=False)
    results = []
    for idx, output in enumerate(outputs):
        raw_text = "{" + output.outputs[0].text
        append_raw_log(f"PHASE2_RAW_{cores[idx]['id']}", raw_text)
        results.append(extract_first_json_object(raw_text))
    return results

def run_phase3_single(core: Dict[str, Any], bad_dialogue: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    prompt = create_phase3_prompt(core, bad_dialogue)
    sampling_params = SamplingParams(temperature=0.55, top_p=0.90, max_tokens=900, stop=["<|im_end|>"])
    output = llm.generate([prompt], sampling_params, use_tqdm=False)[0]
    raw_text = "{" + output.outputs[0].text
    append_raw_log(f"PHASE3_RAW_{core['id']}", raw_text)
    return extract_first_json_object(raw_text)

# =========================================================
# 8. MAIN PIPELINE
# =========================================================

def generate_dataset(total_samples: int = TOTAL_SAMPLES):
    start_time = time.time()
    valid_data = []
    rejected_data = []

    cores = run_phase1(total_samples)
    if not cores: return

    print(f"\n🧩 Phase 2/3: Building dialogues and sanitizing...")
    for batch_start in range(0, len(cores), BATCH_SIZE):
        batch_cores = cores[batch_start: batch_start + BATCH_SIZE]
        batch_outputs = run_phase2_batch(batch_cores)

        for core, phase2_out in zip(batch_cores, batch_outputs):
            sample_id = core["id"]
            dialogue = phase2_out.get("dialogue", []) if isinstance(phase2_out, dict) else []

            # 檢查與清洗
            if is_valid_final_sample(core, dialogue):
                final_item = dict(core)
                final_item["dialogue"] = dialogue
                valid_data.append(final_item)
                print(f"✅ Phase2 OK (Sanitized): {sample_id} | Final Ans: {dialogue[-1]['content']}")
                continue

            print(f"⚠️ Phase2 invalid or too verbose: {sample_id}, entering Phase 3 repair...")

            # Phase 3 repair
            repaired_success = False
            for _ in range(MAX_RETRY_PHASE3):
                repaired_out = run_phase3_single(core, dialogue)
                repaired_dialogue = repaired_out.get("dialogue", []) if repaired_out else []

                if is_valid_final_sample(core, repaired_dialogue):
                    final_item = dict(core)
                    final_item["dialogue"] = repaired_dialogue
                    valid_data.append(final_item)
                    repaired_success = True
                    print(f"🛠️ Phase3 FIXED (Sanitized): {sample_id} | Final Ans: {repaired_dialogue[-1]['content']}")
                    break

            if not repaired_success:
                print(f"❌ Phase2/3 REJECTED: {sample_id}")
                rejected_data.append({"core": core, "dialogue": dialogue})

    with open(FINAL_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(valid_data, f, ensure_ascii=False, indent=2)
    with open(REJECT_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(rejected_data, f, ensure_ascii=False, indent=2)

    elapsed = time.time() - start_time
    print(f"\n🎉 DONE in {elapsed:.2f}s\n✅ Valid: {len(valid_data)}\n❌ Rejected: {len(rejected_data)}")
    print(f"📁 Dataset saved to {FINAL_JSON_FILE}")

if __name__ == "__main__":
    generate_dataset(TOTAL_SAMPLES)
