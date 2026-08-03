#!/usr/bin/env python3
"""测试 qodercli --agents 参数，用 Python subprocess 避免 PowerShell 转义问题."""
import json
import subprocess
import sys

QODERCLI = r"C:\Users\2268\AppData\Roaming\npm\qodercli.cmd"
MODEL = "DeepSeek-V4-Flash"

# 构造 agents JSON - 模拟 improve agent
agents = {
    "code-reviewer": {
        "description": "Code review agent that outputs structured JSON",
        "instructions": """You are a code review agent. You MUST output ONLY valid JSON, no markdown, no explanation, no code fences.

Output format:
{
  "issues": [{"line": <int>, "severity": "<high|medium|low>", "description": "<string>"}],
  "summary": "<string>"
}

The first character must be { and the last character must be }."""
    }
}
agents_json = json.dumps(agents)

print(f"agents JSON: {agents_json[:200]}...")
print(f"JSON length: {len(agents_json)}")

cmd = [
    QODERCLI,
    "-p",
    "--model", MODEL,
    "--no-session-persistence",
    "-o", "text",
    "--agents", agents_json,
    "--agent", "code-reviewer",
    "Review this Python code and output JSON:\n\ndef calculate_average(numbers):\n    total = 0\n    for n in numbers:\n        total += n\n    return total / len(numbers)",
]

print(f"\nCommand args:")
for i, arg in enumerate(cmd):
    print(f"  [{i}] {arg[:80]}")

print("\nRunning...")
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
print(result.stdout[:1000])

if result.stderr:
    # Write stderr to file for debugging
    with open('scripts/_agents_stderr.txt', 'w', encoding='utf-8') as f:
        f.write(result.stderr)
    print(f"\nStderr written to scripts/_agents_stderr.txt ({len(result.stderr)} chars)")
    # Also print first 500 chars with replacement
    stderr_safe = result.stderr.encode('ascii', errors='replace').decode('ascii')
    print(f"Stderr preview: {stderr_safe[:500]}")
