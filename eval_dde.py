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
    return model.half().eval().to(args.device), tokenizer

def run_dde_test(model, tokenizer, context, query, args, test_name="Custom"):
    print(f"\n{'='*20} DDE Memory Test: {test_name} {'='*20}")
    
    # 1. 準備輸入
    # Context (事實注入)
    ctx_prompt = f"{tokenizer.bos_token}user\n事實：{context}\n"
    # Query (問題提問)
    q_prompt = f"user\n問題：{query}\nassistant\n"
    
    # 計算 split_idx (Context 的結束位置)
    ctx_ids = tokenizer(ctx_prompt, add_special_tokens=False).input_ids
    split_idx = len(ctx_ids)
    
    full_prompt = ctx_prompt + q_prompt
    inputs = tokenizer(full_prompt, return_tensors="pt").to(args.device)
    
    print(f"Context Length: {split_idx} tokens")
    print(f"Total Length: {inputs.input_ids.shape[1]} tokens")
    print(f"Memory Path: Force Active (The Wall of Sighs is ON)")
    print(f"💬: {query}")
    print("🧠: ", end='', flush=True)

    streamer = TextStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True)
    
    # 2. 生成 (注意：為了確保嘆息之牆生效，DDE 評估建議關閉 KV Cache 或確保 Mask 正確傳遞)
    # 在這裡我們關閉 use_cache 以進行最嚴格的物理隔離測試
    with torch.no_grad():
        generated_ids = model.generate(
            inputs=inputs["input_ids"],
            max_new_tokens=args.max_new_tokens,
            do_sample=True,
            temperature=args.temperature,
            top_p=args.top_p,
            split_idx=torch.tensor([split_idx], device=args.device), # 傳入關鍵的切分點
            dde_temp=args.dde_eval_temp, # 使用較低的評估溫度
            use_cache=False,  # 強制重新計算以套用 Chunked Mask
            streamer=streamer
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

    # 內建測試集
    needle_tests = [
        {
            "context": "小明的手機號碼是 138-9999-8888。他住在北京市朝陽區。",
            "query": "請告訴我小明的手機號碼是多少？",
            "name": "手機號碼檢索"
        },
        {
            "context": "這是一條秘密指令：今天的暗號是「綠色森林」。請不要告訴任何人。",
            "query": "今天的暗號是什麼？",
            "name": "秘密暗號檢索"
        },
        {
            "context": "在遙遠的亞特蘭提斯，有一種生物叫做「咕嚕喵」，它們只吃藍色的蘋果。",
            "query": "咕嚕喵喜歡吃什麼顏色的蘋果？",
            "name": "虛構事實記憶"
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
