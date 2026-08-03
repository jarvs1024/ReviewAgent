#!/usr/bin/env python3
"""测试 qodercli --agents: 直接用 node 运行 qodercli.js."""
import json
import subprocess
import sys

NODE = r"C:\Program Files\nodejs\node.exe"
QODERCLI_JS = r"C:\Users\2268\AppData\Roaming\npm\node_modules\@qoder-ai\qodercli\bundle\qodercli.js"
MODEL = "DeepSeek-V4-Flash"

# 构造 agents JSON
agents = {
    "code-reviewer": {
        "description": "Code review agent",
        "instructions": "You are a code review agent. Output ONLY valid JSON. Format: {\"issues\": [{\"line\": <int>, \"severity\": \"<high|medium|low>\", \"description\": \"<string>\"}], \"summary\": \"<string>\"}"
    }
}
agents_json = json.dumps(agents)

prompt = "Review this code and output JSON:\n\ndef calculate_average(numbers):\n    total = 0\n    for n in numbers:\n        total += n\n    return total / len(numbers)"

# 直接用 node 运行 qodercli.js
cmd = [
    NODE,
    QODERCLI_JS,
    "-p",
    "--model", MODEL,
    "--no-session-persistence",
    "-o", "text",
    "--agents", agents_json,
    "--agent", "code-reviewer",
    prompt,
]

print(f"Using node directly: {NODE}")
print(f"agents JSON length: {len(agents_json)}")
print(f"prompt length: {len(prompt)}")

result = subprocess.run(
    cmd,
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
    print(f"\nStderr ({len(result.stderr)} chars):")
    stderr_safe = result.stderr.encode('ascii', errors='replace').decode('ascii')
    print(stderr_safe[:500])
