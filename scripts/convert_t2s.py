import json
import os
import argparse
from opencc import OpenCC

def convert_jsonl(input_path, output_path, config='t2s'):
    """
    config options:
    't2s': Traditional Chinese to Simplified Chinese
    's2t': Simplified Chinese to Traditional Chinese
    """
    cc = OpenCC(config)
    
    if not os.path.exists(input_path):
        print(f"Error: File {input_path} not found.")
        return

    with open(input_path, 'r', encoding='utf-8') as f:
        # 判斷是 JSON array 還是 JSONL (DDE 目前是用 JSON array)
        try:
            data = json.load(f)
            is_array = True
        except json.JSONDecodeError:
            f.seek(0)
            data = f.readlines()
            is_array = False

    converted_count = 0
    new_data = []

    if is_array:
        for item in data:
            if 'dialogue' in item:
                for msg in item['dialogue']:
                    msg['content'] = cc.convert(msg['content'])
            new_data.append(item)
            converted_count += 1
    else:
        for line in data:
            item = json.loads(line)
            if 'dialogue' in item:
                for msg in item['dialogue']:
                    msg['content'] = cc.convert(msg['content'])
            elif 'conversations' in item:
                for msg in item['conversations']:
                    msg['content'] = cc.convert(msg['content'])
            elif 'text' in item:
                item['text'] = cc.convert(item['text'])
            
            new_data.append(item)
            converted_count += 1

    with open(output_path, 'w', encoding='utf-8') as f:
        if is_array:
            json.dump(new_data, f, ensure_ascii=False, indent=2)
        else:
            for item in new_data:
                f.write(json.dumps(item, ensure_ascii=False) + '\n')

    print(f"Successfully converted {converted_count} samples to {output_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert dataset between Traditional and Simplified Chinese")
    parser.add_argument("--input", type=str, default="dataset/dde-v1.jsonl", help="Input file path")
    parser.add_argument("--output", type=str, default="dataset/dde-v1-s.jsonl", help="Output file path")
    parser.add_argument("--mode", type=str, default="t2s", choices=['t2s', 's2t'], help="t2s or s2t")
    
    args = parser.parse_args()
    
    # 確保輸出目錄存在
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    
    convert_jsonl(args.input, args.output, args.mode)
