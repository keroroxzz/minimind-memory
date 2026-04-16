from numpy.strings import index
import torch
from torch.utils.data import Dataset
from datasets import load_dataset
import random

class ReasoningDataset(Dataset):
    def __init__(self, tokenizer, max_length=512, split="train", max_samples=None):
        self.tokenizer = tokenizer
        self.max_length = max_length
        self.data = []
        
        print(f"📥 載入並處理【簡體中文】推理資料集 (過濾長度 > {max_length})...")
        
        # 1. 載入 BelleGroup 數學應用題 (簡體中文 CoT)
        self._load_and_process(
            "BelleGroup/school_math_0.25M", 
            split, max_samples, self._process_belle_math
        )
        
        # 2. 載入 MATH
        self._load_and_process(
            "HuggingFaceH4/MATH", 
            split, max_samples, self._process_MATH
        )

        # 2. 載入 繁中 中文數學題
        self._load_and_process(
            "twinkle-ai/tw-math-reasoning-2k", 
            split, max_samples, self._process_MATH
        )

        print(f"✅ 簡中資料集準備完畢，有效樣本數: {len(self.data)}")
        random.shuffle(self.data)

    def _load_and_process(self, repo, split, limit, process_func):
        try:
            print(f"處理 {repo}...")
            ds = load_dataset(repo, split=split)
            process_func(ds, limit)
        except Exception as e:
            print(f"⚠️ {repo} 失敗: {e}")

    def _add_valid_sample(self, prompt, response):
        """長度硬過濾，確保邏輯鏈完整"""
        if not prompt or not response: return

        prompt = prompt.strip()
        response = response.strip()

        inputs_prompt = self.tokenizer.bos_token + prompt
        inputs_prompt = self.tokenizer(inputs_prompt, add_special_tokens=False, return_tensors="pt", truncation=True)
        input_prompt_ids = inputs_prompt["input_ids"]

        response_prompt = response + self.tokenizer.eos_token
        response_prompt = self.tokenizer(response_prompt, add_special_tokens=False, return_tensors="pt", truncation=True)
        response_prompt_ids = response_prompt["input_ids"]

        encoded_len = len(input_prompt_ids[0]) + len(response_prompt_ids[0])

        if encoded_len <= self.max_length + 10:
            self.data.append({"prompt": prompt.strip(), "response": response.strip(), "prompt_len": len(input_prompt_ids[0])})

    # === 解析函數 (簡體中文資料集專用) ===
    
    def _process_belle_math(self, ds, limit):
        for i, item in enumerate(ds):
            if limit and i >= limit: break
            prompt = item['instruction']
            if item.get('input'):
                prompt += "\n" + item['input']
            self._add_valid_sample(prompt, item['output'])

    def _process_MATH(self, ds, limit):
        for i, item in enumerate(ds):
            if limit and i >= limit: break
            prompt = f"{item['problem']}"
            response = f"\n{item['solution']}"
            self._add_valid_sample(prompt, response)

    def __len__(self): return len(self.data)

    def __getitem__(self, idx):
        sample = self.data[idx]

        inputs_prompt = self.tokenizer.bos_token + sample['prompt']
        input_prompt_ids = self.tokenizer(inputs_prompt, add_special_tokens=False, return_tensors="pt", truncation=True).input_ids

        response_prompt = sample['response'] + self.tokenizer.eos_token
        response_prompt_ids = self.tokenizer(response_prompt, add_special_tokens=False, return_tensors="pt", truncation=True).input_ids

        input_ids = torch.cat([input_prompt_ids[0], response_prompt_ids[0]]).tolist()
        if len(input_ids) > self.max_length:
            input_ids = input_ids[:self.max_length]
        else:
            input_ids = input_ids + [self.tokenizer.pad_token_id] * (self.max_length - len(input_ids))
        input_ids = torch.tensor(input_ids, dtype=torch.long)
        labels = input_ids.clone()
        labels[:len(input_prompt_ids[0])] = -100

        return input_ids, labels

    def __getitem_chat__(self, idx):
        item = self.data[idx]
        messages = [
            {"role": "user", "content": item['prompt']},
            {"role": "assistant", "content": item['response']}
        ]
        
        # 使用 apply_chat_template 獲取完整 ID
        input_ids = self.tokenizer.apply_chat_template(
            messages, 
            tokenize=True, 
            add_generation_prompt=False, 
            truncation=True, 
            max_length=self.max_length + 1, 
            padding="max_length"
        )
        input_ids = torch.tensor(input_ids)
        
        x = input_ids[:-1]
        y = input_ids[1:].clone()
        
        # 忽略 User Prompt 的 Loss
        # 我們需要找出 Assistant 回覆開始的位置
        user_messages = [{"role": "user", "content": item['prompt']}]
        user_prompt_ids = self.tokenizer.apply_chat_template(
            user_messages, 
            tokenize=True, 
            add_generation_prompt=True
        )
        prompt_len = len(user_prompt_ids)
        
        # 將 prompt 部分的 label 設為 -100
        y[:prompt_len-1] = -100
        
        pad_id = self.tokenizer.pad_token_id if self.tokenizer.pad_token_id is not None else 0
        y[y == pad_id] = -100

        return x, y
