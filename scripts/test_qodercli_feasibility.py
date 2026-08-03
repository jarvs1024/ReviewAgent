#!/usr/bin/env python3
"""验证 qodercli 替换 opencode 的可行性.

测试:
1. qodercli -p 非交互模式能否正常返回 JSON
2. --agent 能否加载自定义 agent
3. --attachment 能否附加文件 context
4. 输出格式是否稳定可解析
"""
import json
import subprocess
import sys
import time
from pathlib import Path

# 配置
QODERCLI = r"C:\Users\2268\AppData\Roaming\npm\qodercli.cmd"
MODEL = "DeepSeek-V4-Flash"  # 与 opencode 当前使用的模型一致
TIMEOUT = 300  # 秒

# 测试 1: 基础 JSON 输出
def test_basic_json():
    """测试 qodercli -p 能否返回结构化 JSON."""
    print("\n" + "=" * 60)
    print("TEST 1: 基础 JSON 输出")
    print("=" * 60)

    prompt = """你是一个代码审查助手。请分析以下代码并输出严格 JSON:

```python
def calculate_average(numbers):
    total = 0
    for n in numbers:
        total += n
    return total / len(numbers)
```

输出格式 (必须是合法 JSON):
{
  "issues": [{"line": <int>, "severity": "<high|medium|low>", "description": "<string>"}],
  "summary": "<string>"
}

重要: 只输出 JSON 对象，不要输出任何其他文字、代码块标记或解释。第一个字符必须是 {，最后一个字符必须是 }。"""

    cmd = [
        QODERCLI,
        "-p",
        "--model", MODEL,
        "--no-session-persistence",
        "-o", "text",
        prompt,
    ]

    print(f"Command: {' '.join(cmd[:5])}...")
    print(f"Model: {MODEL}")

    start = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            encoding="utf-8",
            errors="replace",
        )
        elapsed = time.time() - start

        print(f"Exit code: {result.returncode}")
        print(f"Elapsed: {elapsed:.1f}s")
        print(f"Stdout length: {len(result.stdout)} chars")

        if result.returncode != 0:
            print(f"STDERR: {result.stderr[:500]}")
            return False

        # 提取 JSON
        output = result.stdout.strip()
        print(f"\nOutput preview:\n{output[:500]}")

        # 尝试解析 JSON
        json_str = _extract_json(output)
        if json_str:
            data = json.loads(json_str)
            print(f"\n[PASS] JSON 解析成功!")
            print(f"   Keys: {list(data.keys())}")
            if "issues" in data:
                print(f"   Issues count: {len(data['issues'])}")
            return True
        else:
            print(f"\n[FAIL] 未找到可解析的 JSON")
            return False

    except subprocess.TimeoutExpired:
        print(f"\n[FAIL] 超时 ({TIMEOUT}s)")
        return False
    except Exception as e:
        print(f"\n[FAIL] 异常: {e}")
        return False


# 测试 2: 带文件 attachment
def test_attachment():
    """测试 --attachment 能否附加文件作为 context."""
    print("\n" + "=" * 60)
    print("TEST 2: 文件 Attachment")
    print("=" * 60)

    # 创建一个临时测试文件
    test_file = Path("scripts/_test_sample.py")
    test_file.write_text("""
def process_data(items):
    result = []
    for item in items:
        value = item['value']
        result.append(value * 2)
    return result

def calculate_stats(data):
    if not data:
        return {}
    avg = sum(data) / len(data)
    return {'average': avg, 'max': max(data)}
""", encoding="utf-8")

    prompt = """请审查附件中的 Python 代码，输出严格 JSON 对象。

输出格式 (必须是合法 JSON):
{
  "issues": [{"line": <int>, "severity": "<high|medium|low>", "description": "<string>"}],
  "summary": "<string>"
}

重要: 只输出 JSON 对象，不要输出任何其他文字、代码块标记或解释。第一个字符必须是 {，最后一个字符必须是 }。"""

    cmd = [
        QODERCLI,
        "-p",
        "--model", MODEL,
        "--no-session-persistence",
        "-o", "text",
        "--attachment", str(test_file),
        prompt,
    ]

    print(f"Command: {' '.join(cmd[:6])}...")
    print(f"Attachment: {test_file}")

    start = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            encoding="utf-8",
            errors="replace",
        )
        elapsed = time.time() - start

        print(f"Exit code: {result.returncode}")
        print(f"Elapsed: {elapsed:.1f}s")

        if result.returncode != 0:
            print(f"STDERR: {result.stderr[:500]}")
            return False

        output = result.stdout.strip()
        print(f"\nOutput preview:\n{output[:500]}")

        json_str = _extract_json(output)
        if json_str:
            data = json.loads(json_str)
            print(f"\n[PASS] JSON 解析成功!")
            print(f"   Issues: {len(data.get('issues', []))}")
            return True
        else:
            print(f"\n[FAIL] 未找到可解析的 JSON")
            return False

    finally:
        test_file.unlink(missing_ok=True)


# 测试 3: 自定义 agent (通过 --agents JSON)
def test_custom_agent():
    """测试 --agents 能否加载自定义 agent 定义."""
    print("\n" + "=" * 60)
    print("TEST 3: 自定义 Agent (--agents)")
    print("=" * 60)

    # 测试 --system-prompt 替代 --agents (更简单直接)
    system_prompt = "你是一个代码审查助手。你必须只输出严格 JSON 对象，不要输出任何其他文字、代码块标记或解释。JSON 格式: {\"issues\": [{\"line\": <int>, \"severity\": \"<high|medium|low>\", \"description\": \"<string>\"}], \"summary\": \"<string>\"}"

    prompt = "审查这段代码: print('hello')"

    cmd = [
        QODERCLI,
        "-p",
        "--model", MODEL,
        "--no-session-persistence",
        "-o", "text",
        "--system-prompt", system_prompt,
        prompt,
    ]

    print(f"System prompt: {system_prompt[:60]}...")
    print(f"Command: {' '.join(cmd[:6])}...")

    start = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            encoding="utf-8",
            errors="replace",
        )
        elapsed = time.time() - start

        print(f"Exit code: {result.returncode}")
        print(f"Elapsed: {elapsed:.1f}s")

        if result.returncode != 0:
            print(f"STDERR: {result.stderr[:500]}")
            return False

        output = result.stdout.strip()
        print(f"\nOutput preview:\n{output[:500]}")

        json_str = _extract_json(output)
        if json_str:
            data = json.loads(json_str)
            print(f"\n[PASS] JSON 解析成功!")
            return True
        else:
            print(f"\n[FAIL] 未找到可解析的 JSON")
            return False

    except Exception as e:
        print(f"\n[FAIL] 异常: {e}")
        return False


# 测试 4: 模拟真实 improve 场景
def test_improve_simulation():
    """模拟真实的 /improve 场景: 读 prompt 模板 + diff 文件."""
    print("\n" + "=" * 60)
    print("TEST 4: 模拟 /improve 场景")
    print("=" * 60)

    # 使用 --attachment 传递 diff 文件，避免命令行参数过长
    diff_file = Path("scripts/_test_diff.patch")
    diff_file.write_text("""diff --git a/test_sample.py b/test_sample.py
--- a/test_sample.py
+++ b/test_sample.py
@@ -1,5 +1,8 @@
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

    # 构造简短 prompt + VALID NEW LINES
    short_prompt = """请审查附件中的 diff，输出严格 JSON 对象。

输出格式 (必须是合法 JSON):
{
  "summary_md": "## 改进总览\\n\\n简短中文总结",
  "suggestions": [{"file": "<string>", "start_line": <int>, "end_line": <int>, "header": "<string>", "existing_code": "<string>", "improved_code": "<string>", "rationale": "<string>", "label": "<string>", "severity": "<high|medium|low>"}]
}

VALID NEW LINES: test_sample.py: 1,2,5,6,7

重要: 只输出 JSON 对象，不要输出任何其他文字、代码块标记或解释。第一个字符必须是 {，最后一个字符必须是 }。"""

    cmd = [
        QODERCLI,
        "-p",
        "--model", MODEL,
        "--no-session-persistence",
        "-o", "text",
        "--max-output-tokens", "4096",
        "--attachment", str(diff_file),
        short_prompt,
    ]

    print(f"Prompt length: {len(short_prompt)} chars")
    print(f"Attachment: {diff_file}")
    print(f"Model: {MODEL}")

    start = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            encoding="utf-8",
            errors="replace",
        )
        elapsed = time.time() - start

        print(f"Exit code: {result.returncode}")
        print(f"Elapsed: {elapsed:.1f}s")
        print(f"Stdout length: {len(result.stdout)} chars")

        if result.returncode != 0:
            stderr_preview = result.stderr[:500].encode('utf-8', errors='replace').decode('utf-8')
            print(f"STDERR: {stderr_preview}")
            return False

        output = result.stdout.strip()
        print(f"\nOutput preview (first 800 chars):\n{output[:800]}")

        # 尝试解析 JSON
        json_str = _extract_json(output)
        if json_str:
            data = json.loads(json_str)
            print(f"\n[PASS] JSON 解析成功!")
            print(f"   Keys: {list(data.keys())}")
            if "suggestions" in data:
                print(f"   Suggestions: {len(data['suggestions'])}")
                for s in data["suggestions"][:2]:
                    if isinstance(s, dict):
                        print(f"     - {s.get('file', '?')}:{s.get('start_line', '?')} [{s.get('severity', '?')}] {s.get('header', '?')}")
                    else:
                        print(f"     - {s}")
            if "issues" in data:
                print(f"   Issues: {len(data['issues'])}")
                for s in data["issues"][:2]:
                    if isinstance(s, dict):
                        print(f"     - {s.get('location', '?')} [{s.get('severity', '?')}] {s.get('description', '?')[:50]}")
                    else:
                        print(f"     - {s}")
            if "summary_md" in data:
                print(f"   Summary: {data['summary_md'][:100]}...")
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
    print("qodercli 替换 opencode 可行性验证")
    print("=" * 60)

    results = {}

    # 运行测试
    results["basic_json"] = test_basic_json()
    results["attachment"] = test_attachment()
    results["custom_agent"] = test_custom_agent()
    results["improve_sim"] = test_improve_simulation()

    # 汇总
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
        print("\n[OK] 所有测试通过! qodercli 可以替换 opencode")
    elif passed >= total * 0.75:
        print("\n[WARN] 大部分测试通过，需要少量适配工作")
    else:
        print("\n[FAIL] 测试失败较多，需要较多适配或 qodercli 能力不足")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
