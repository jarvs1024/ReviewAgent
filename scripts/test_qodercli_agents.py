#!/usr/bin/env python3
"""测试 qodercli 的 --agents 参数定义自定义 agent."""
import json
import subprocess
import sys
from pathlib import Path

QODERCLI = r"C:\Users\2268\AppData\Roaming\npm\qodercli.cmd"
MODEL = "DeepSeek-V4-Flash"

# 定义自定义 agents (类似 opencode 的 prompts/*.md)
agents_json = json.dumps({
    "code-reviewer": {
        "description": "Code review agent that outputs structured JSON",
        "system_prompt": """你是一个代码审查助手。你必须只输出严格 JSON 对象，不要输出任何其他文字、代码块标记或解释。

输出格式:
{
  "issues": [{"line": <int>, "severity": "<high|medium|low>", "description": "<string>"}],
  "summary": "<string>"
}

第一个字符必须是 {，最后一个字符必须是 }。""",
    },
    "improve-agent": {
        "description": "Code improvement agent for GitLab suggestions",
        "system_prompt": """你是一个代码改进助手。分析 diff 并输出可 Apply 的改进建议。

输出格式 (严格 JSON):
{
  "summary_md": "## 改进总览\\n\\n简短总结",
  "suggestions": [
    {
      "file": "<string>",
      "start_line": <int>,
      "end_line": <int>,
      "header": "<string>",
      "existing_code": "<string>",
      "improved_code": "<string>",
      "rationale": "<string>",
      "label": "<string>",
      "severity": "<high|medium|low>"
    }
  ]
}

第一个字符必须是 {，最后一个字符必须是 }。""",
    }
})

def test_agents_param():
    """测试 --agents + --agent 参数."""
    print("=" * 60)
    print("TEST: --agents + --agent 参数")
    print("=" * 60)

    cmd = [
        QODERCLI,
        "-p",
        "--model", MODEL,
        "--no-session-persistence",
        "-o", "text",
        "--agents", agents_json,
        "--agent", "code-reviewer",
        "审查这段代码: def foo(x): return x + 1",
    ]

    print(f"Command: {' '.join(cmd[:8])}...")
    print(f"Agent: code-reviewer")

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
        encoding="utf-8",
        errors="replace",
    )

    print(f"Exit code: {result.returncode}")
    if result.returncode != 0:
        stderr = result.stderr[:500].encode('ascii', errors='replace').decode('ascii')
        print(f"STDERR: {stderr}")
        return False

    output = result.stdout.strip()
    print(f"\nOutput:\n{output[:1000]}")

    # 解析 JSON
    json_str = _extract_json(output)
    if json_str:
        data = json.loads(json_str)
        print(f"\n[PASS] JSON 解析成功!")
        print(f"   Keys: {list(data.keys())}")
        return True
    else:
        print(f"\n[FAIL] 未找到可解析的 JSON")
        return False


def test_improve_agent():
    """测试 improve-agent."""
    print("\n" + "=" * 60)
    print("TEST: improve-agent")
    print("=" * 60)

    # 创建 diff 文件
    diff_file = Path("scripts/_test_diff2.patch")
    diff_file.write_text("""diff --git a/test.py b/test.py
--- a/test.py
+++ b/test.py
@@ -1,3 +1,5 @@
+import os
+
 def process(items):
     result = []
     for item in items:
-        result.append(item * 2)
+        value = item['data']
+        result.append(value * 2)
     return result
""", encoding="utf-8")

    cmd = [
        QODERCLI,
        "-p",
        "--model", MODEL,
        "--no-session-persistence",
        "-o", "text",
        "--agents", agents_json,
        "--agent", "improve-agent",
        "--attachment", str(diff_file),
        "审查附件中的 diff，输出改进建议",
    ]

    print(f"Agent: improve-agent")
    print(f"Attachment: {diff_file}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            encoding="utf-8",
            errors="replace",
        )

        print(f"Exit code: {result.returncode}")
        if result.returncode != 0:
            stderr = result.stderr[:500].encode('ascii', errors='replace').decode('ascii')
            print(f"STDERR: {stderr}")
            return False

        output = result.stdout.strip()
        print(f"\nOutput:\n{output[:1000]}")

        json_str = _extract_json(output)
        if json_str:
            data = json.loads(json_str)
            print(f"\n[PASS] JSON 解析成功!")
            print(f"   Keys: {list(data.keys())}")
            if "suggestions" in data:
                print(f"   Suggestions: {len(data['suggestions'])}")
            return True
        else:
            print(f"\n[FAIL] 未找到可解析的 JSON")
            return False

    finally:
        diff_file.unlink(missing_ok=True)


def _extract_json(text: str) -> str | None:
    """从文本中提取 JSON 对象."""
    import re

    # 1. 尝试 ```json ... ``` 围栏
    fence = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    # 2. 整段尝试
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            json.loads(stripped)
            return stripped
        except json.JSONDecodeError:
            pass

    # 3. 找第一个 { 到最后一个 }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start:end + 1]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    return None


def main():
    print("=" * 60)
    print("qodercli --agents 自定义 agent 测试")
    print("=" * 60)

    results = {}
    results["agents_param"] = test_agents_param()
    results["improve_agent"] = test_improve_agent()

    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    for name, passed in results.items():
        status = "[PASS]" if passed else "[FAIL]"
        print(f"  {name:20s} {status}")

    total = len(results)
    passed = sum(1 for v in results.values() if v)
    print(f"\n总计: {passed}/{total} 通过")

    if passed == total:
        print("\n[OK] --agents 参数可用! 可以替代 --system-prompt")
    else:
        print("\n[FAIL] --agents 参数有问题")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
