import time
import argparse
import random
import warnings
import torch
from transformers import AutoTokenizer, TextStreamer
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM
from trainer.trainer_utils import setup_seed, get_model_params, get_model_paths

warnings.filterwarnings('ignore')

def init_model(args):
    tokenizer = AutoTokenizer.from_pretrained(args.load_from)
    model = MiniMindForCausalLM(MiniMindConfig(
        hidden_size=args.hidden_size,
        num_hidden_layers=args.num_hidden_layers,
        use_moe=bool(args.use_moe),
        use_engram=bool(args.use_engram),
        use_dde=True,  # 評估模式強制開啟 DDE
        dde_layer=args.dde_layer
    ))
    ckp_path, _ = get_model_paths(args.save_dir, args.weight, model.config)
    state_dict = torch.load(ckp_path, map_location=args.device)
    model.load_state_dict(state_dict, strict=False)
    get_model_params(model, model.config)
    return model.eval().to(args.device), tokenizer

def format_msgs(msgs):
    prompt = ""
    for msg in msgs:
        role = msg['role']
        content = msg['content']
        prompt += f"<|im_start|>{role}\n{content}<|im_end|>\n"
    return prompt

def run_dde_test(model, tokenizer, context, query, args, test_name="Custom"):
    print(f"\n{'='*20} DDE Memory Test: {test_name} {'='*20}")
    
    # [情境預熱] 模擬真實對話，讓 DDE 有足夠的寫入窗口
    dialogue = [
        {"role": "user", "content": "你好，请记住我接下来要说的一些重要信息。"},
        {"role": "assistant", "content": "好的，我会认真记住您提供的所有事实。请说。"},
        {"role": "user", "content": f"事实：{context}"}
    ]
    
    ctx_prompt = format_msgs(dialogue)
    # Query 包含問題
    q_msgs = [
        {"role": "user", "content": f"问题：{query}"}
    ]
    q_prompt = format_msgs(q_msgs) + "<|im_start|>assistant\n"
    
    # 計算 split_idx (Context 的結束位置)
    ctx_ids = tokenizer(ctx_prompt, add_special_tokens=False).input_ids
    split_idx = len(ctx_ids)
    
    full_prompt = ctx_prompt + q_prompt
    inputs = tokenizer(full_prompt, return_tensors="pt").to(args.device)
    
    print(f"Context Length: {split_idx} tokens")
    print(f"Memory Path: Force Active (The Wall of Sighs is ON)")
    print(f"💬: {query}")
    print("🧠: ", end='', flush=True)

    streamer = TextStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True)
    
    with torch.no_grad():
        generated_ids = model.generate(
            inputs=inputs["input_ids"],
            max_new_tokens=args.max_new_tokens,
            do_sample=False, # [確定性優化] 關閉隨機性，直接看最強記憶信號
            split_idx=torch.tensor([split_idx], device=args.device),
            dde_temp=args.dde_eval_temp,
            use_cache=False,
            streamer=streamer,
            eos_token_id=tokenizer.eos_token_id
        )
    print("\n" + "="*60)

def main():
    parser = argparse.ArgumentParser(description="MiniMind DDE 記憶模組專屬評估工具")
    parser.add_argument('--load_from', default='model', type=str)
    parser.add_argument('--save_dir', default='out', type=str)
    parser.add_argument('--weight', default='dde_v1', type=str, help="DDE 權重名稱")
    parser.add_argument('--hidden_size', default=768, type=int)
    parser.add_argument('--num_hidden_layers', default=8, type=int)
    parser.add_argument('--use_moe', default=0, type=int)
    parser.add_argument('--use_engram', default=0, type=int)
    parser.add_argument('--device', default='cuda' if torch.cuda.is_available() else 'cpu', type=str)
    
    parser.add_argument('--dde_layer', default=4, type=int)
    parser.add_argument('--dde_eval_temp', default=0.1, type=float, help="記憶檢索溫度 (建議設低以獲得確定性結果)")
    parser.add_argument('--temperature', default=0.7, type=float)
    parser.add_argument('--top_p', default=0.9, type=float)
    parser.add_argument('--max_new_tokens', default=128, type=int)

    args = parser.parse_args()
    model, tokenizer = init_model(args)
    setup_seed(42)

    # 内建测试集 (简体中文)
    needle_tests = [
        {
            "context": "小明的手机号码是 138-9999-8888。他住在北京市朝阳区。",
            "query": "请告诉我小明的手机号码是多少？",
            "name": "手机号码检索"
        },
        {
            "context": "这是一条秘密指令：今天的暗号是「绿色森林」。请不要告诉任何人。",
            "query": "今天的暗号是什么？",
            "name": "秘密暗号检索"
        },
        {
            "context": "在遥远的亚特兰蒂斯，有一种生物叫做「咕噜喵」，它们只吃蓝色的苹果。",
            "query": "咕噜喵喜欢吃什么颜色的苹果？",
            "name": "虚构事实记忆"
        }
    ]

    print("\nMiniMind DDE-v1 記憶能力專屬評測啟動...")
    print("模式：強制物理隔離模式。Query 區塊完全無法直接 Attention 到 Context 區塊。")
    print("模型必須且僅能通過 DDE 記憶模塊傳遞事實。")

    while True:
        print("\n請選擇測試模式：")
        print("[0] 自動運行內建針尖測試 (Needle Tests)")
        print("[1] 手動輸入自定義測試")
        print("[q] 退出")
        choice = input("選擇: ")

        if choice == '0':
            for test in needle_tests:
                run_dde_test(model, tokenizer, test['context'], test['query'], args, test['name'])
        elif choice == '1':
            ctx = input("請輸入 Context (要記憶的事實): ")
            qry = input("請輸入 Query (針對事實的提問): ")
            run_dde_test(model, tokenizer, ctx, qry, args, "手動測試")
        elif choice == 'q':
            break

if __name__ == "__main__":
    main()
