import torch
from torch.utils.data import Dataset
from datasets import load_dataset
import random

class ReasoningDataset(Dataset):
    def __init__(self, tokenizer, max_length=512, split="train", max_samples=None):
        self.tokenizer = tokenizer
        self.max_length = max_length
        self.data = []
        
        print("📥 載入並處理 Reasoning 資料集 (過濾長度 > {})...".format(max_length))
        
        # 載入並處理資料集
        self._load_and_process("yuhuanstudio/gsm8k_zhtw", split, max_samples, self._process_gsm8k)
        self._load_and_process("meta-math/MetaMathQA", split, max_samples, self._process_metamath)
        self._load_and_process("ucinlp/drop", split, max_samples, self._process_drop)
        self._load_and_process("lucasmccabe/logiqa", split, max_samples, self._process_logiqa)
        self._load_and_process("voidful/ReClor", split, max_samples, self._process_reclor)

        print(f"✅ 資料集準備完畢，有效樣本數: {len(self.data)}")
        random.shuffle(self.data)

    def _load_and_process(self, repo, split, limit, process_func):
        try:
            print(f"處理 {repo}...")
            # 注意: 部分資料集可能沒有 "train" 切分或需要特定參數，這裡使用默認邏輯
            ds = load_dataset(repo, split=split)
            process_func(ds, limit)
        except Exception as e:
            print(f"⚠️ {repo} 失敗: {e}")

    def _add_valid_sample(self, prompt, response):
        """過濾掉編碼後超過 max_length 的樣本以保證邏輯完整性"""
        if not prompt or not response: return
        
        text = f"User: {prompt.strip()}\n\nAssistant: {response.strip()}"
        encoded_len = len(self.tokenizer.encode(text, add_special_tokens=True))
        
        # 保留空間給 shift target (+1)
        if encoded_len <= self.max_length + 1:
            self.data.append({"prompt": prompt.strip(), "response": response.strip()})

    # === 解析函數 ===
    def _process_gsm8k(self, ds, limit):
        for i, item in enumerate(ds):
            if limit and i >= limit: break
            self._add_valid_sample(item['question'], item['answer'])

    def _process_metamath(self, ds, limit):
        for i, item in enumerate(ds):
            if limit and i >= limit: break
            self._add_valid_sample(item['query'], item['response'])

    def _process_drop(self, ds, limit):
        for i, item in enumerate(ds):
            if limit and i >= limit: break
            ans = item.get('answers_spans', {}).get('spans', [])
            if ans:
                self._add_valid_sample(f"Passage:\n{item['passage']}\n\nQ: {item['question']}", ans[0])

    def _process_logiqa(self, ds, limit):
        for i, item in enumerate(ds):
            if limit and i >= limit: break
            opts = "\n".join([f"{chr(65+j)}. {opt}" for j, opt in enumerate(item['options'])])
            ans_idx = item['label']
            if isinstance(ans_idx, int) and 0 <= ans_idx < len(item['options']):
                ans_text = f"{chr(65+ans_idx)}. {item['options'][ans_idx]}"
                self._add_valid_sample(f"Context:\n{item['context']}\n\nQ: {item['question']}\n{opts}", ans_text)

    def _process_reclor(self, ds, limit):
        for i, item in enumerate(ds):
            if limit and i >= limit: break
            opts = "\n".join([f"{chr(65+j)}. {opt}" for j, opt in enumerate(item['answers'])])
            ans_idx = item['label']
            if isinstance(ans_idx, int) and 0 <= ans_idx < len(item['answers']):
                ans_text = f"{chr(65+ans_idx)}. {item['answers'][ans_idx]}"
                self._add_valid_sample(f"Context:\n{item['context']}\n\nQ: {item['question']}\n{opts}", ans_text)

    def __len__(self): return len(self.data)

    def __getitem__(self, idx):
        item = self.data[idx]
        text = f"User: {item['prompt']}\n\nAssistant: {item['response']}"
        
        encoded = self.tokenizer(text, truncation=True, max_length=self.max_length + 1, padding="max_length", return_tensors="pt")
        input_ids = encoded["input_ids"].squeeze(0)
        
        x = input_ids[:-1]
        y = input_ids[1:].clone()
        
        # 忽略 User Prompt 的 Loss (-100)
        prompt_text = f"User: {item['prompt']}\n\nAssistant:"
        prompt_encoded = self.tokenizer(prompt_text, add_special_tokens=False)
        prompt_len = len(prompt_encoded["input_ids"])
        y[:prompt_len-1] = -100
        
        pad_id = self.tokenizer.pad_token_id if self.tokenizer.pad_token_id is not None else 0
        y[y == pad_id] = -100

        return x, y
