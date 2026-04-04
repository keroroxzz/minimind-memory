import os
import sys
import torch

# 確保能找到根目錄的 model 套件
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from transformers import AutoTokenizer, TextStreamer
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM
from trainer.trainer_utils import get_model_paths

def probe():
    model_path = "minimind-3"
    device = "cuda" if torch.cuda.is_available() else "cpu"
    
    print(f"正在載入基礎模型權重: {model_path}")
    tokenizer = AutoTokenizer.from_pretrained(model_path)
    
    # 載入模型配置 (不開啟 DDE，純探測基礎能力)
    config = MiniMindConfig.from_pretrained(model_path)
    config.use_dde = False 
    model = MiniMindForCausalLM(config)
    
    # 載入權重
    ckp_path = "minimind-3/model.safetensors"
    from safetensors.torch import load_file
    state_dict = load_file(ckp_path)
    model.load_state_dict(state_dict, strict=False)
    model = model.eval().to(device)
    
    test_prompts = [
        {
            "name": "1. 純文本探測 (無格式)",
            "text": "你好，请问你是谁？"
        },
        {
            "name": "2. 標準對話格式 (im_start)",
            "text": "<|im_start|>user\n你好，请问你是谁？<|im_end|>\n<|im_start|>assistant\n"
        },
        {
            "name": "3. 推理誘導測試 (強制開啟 think)",
            "text": "<|im_start|>user\n1+1等于几？<|im_end|>\n<|im_start|>assistant\n<think>\n"
        }
    ]

    streamer = TextStreamer(tokenizer, skip_prompt=True, skip_special_tokens=False)

    print("\n" + "="*50)
    print("MiniMind-3 基礎權重行為探測")
    print("="*50)

    for prompt in test_prompts:
        print(f"\n測試項目: {prompt['name']}")
        print(f"輸入 Prompt: {repr(prompt['text'])}")
        print("模型輸出: ", end="")
        
        input_ids = tokenizer(prompt['text'], return_tensors="pt").input_ids.to(device)
        
        with torch.no_grad():
            model.generate(
                input_ids,
                max_new_tokens=128,
                do_sample=True,
                temperature=0.7,
                top_p=0.9,
                streamer=streamer,
                eos_token_id=tokenizer.eos_token_id
            )
        print("\n" + "-"*30)

if __name__ == "__main__":
    probe()
