#!/usr/bin/env python3
"""测试 qodercli --agents: 用 stdin 传递 prompt 避免命令行长度限制."""
import json
import subprocess
import sys

QODERCLI = r"C:\Users\2268\AppData\Roaming\npm\qodercli.cmd"
MODEL = "DeepSeek-V4-Flash"

# 构造 agents JSON
agents = {
    "code-reviewer": {
        "description": "Code review agent",
        "instructions": "You are a code review agent. Output ONLY valid JSON. First char must be { last char must be }. Format: {\"issues\": [{\"line\": <int>, \"severity\": \"<high|medium|low>\", \"description\": \"<string>\"}], \"summary\": \"<string>\"}"
    }
}
agents_json = json.dumps(agents)

prompt = "Review this code and output JSON:\n\ndef calculate_average(numbers):\n    total = 0\n    for n in numbers:\n        total += n\n    return total / len(numbers)"

# 方法 1: 用 stdin 传递 prompt (qodercli 支持 - 后缀读 stdin)
cmd = [
    QODERCLI,
    "-p",
    "--model", MODEL,
    "--no-session-persistence",
    "-o", "text",
    "--agents", agents_json,
    "--agent", "code-reviewer",
    "-",  # 从 stdin 读取
]

print(f"agents JSON length: {len(agents_json)}")
print(f"prompt length: {len(prompt)}")
print(f"Using stdin to pass prompt...")

result = subprocess.run(
    cmd,
    input=prompt,
    capture_output=True,
    text=True,
    timeout=120,
    encoding="utf-8",
    errors="replace",
)

print(f"\nExit code: {result.returncode}")
print(f"Stdout ({len(result.stdout)} chars):")
print(result.stdout[:1500])

if result.returncode != 0 and result.stderr:
    with open('scripts/_agents_stderr2.txt', 'w', encoding='utf-8') as f:
        f.write(result.stderr)
    print(f"\nStderr ({len(result.stderr)} chars) written to _agents_stderr2.txt")
