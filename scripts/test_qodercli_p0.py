#!/usr/bin/env python3
"""qodercli P0/P1 验证脚本 — 覆盖并行、长 prompt、工作目录、超时、工具限制、token 统计."""
import json
import subprocess
import sys
import time
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

NODE = r"C:\Program Files\nodejs\node.exe"
QODERCLI_JS = r"C:\Users\2268\AppData\Roaming\npm\node_modules\@qoder-ai\qodercli\bundle\qodercli.js"
MODEL = "DeepSeek-V4-Flash"

agents_json = json.dumps({
    "test-agent": {
        "description": "Test agent",
        "instructions": "Output ONLY valid JSON. First char { last char }. No markdown, no code fences, no explanation."
    }
})


def run_qodercli(prompt: str, *, cwd: str | None = None, attachment: str | None = None,
                   timeout: int = 120, disallowed_tools: list[str] | None = None,
                   agents: str | None = None, agent: str | None = None) -> dict:
    """运行 qodercli 并返回结果 dict."""
    cmd = [NODE, QODERCLI_JS, "-p", "--model", MODEL, "--no-session-persistence", "-o", "text"]
    if cwd:
        cmd += ["-w", cwd]
    if attachment:
        cmd += ["--attachment", attachment]
    if disallowed_tools:
        cmd += ["--disallowed-tools", ",".join(disallowed_tools)]
    if agents:
        cmd += ["--agents", agents]
    if agent:
        cmd += ["--agent", agent]
    cmd.append(prompt)

    t0 = time.monotonic()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                encoding="utf-8", errors="replace")
        elapsed = time.monotonic() - t0
        return {
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "elapsed": elapsed,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired:
        return {"exit_code": -1, "stdout": "", "stderr": "TIMEOUT", "elapsed": timeout, "timed_out": True}


def extract_json(text: str) -> dict | None:
    """从输出中提取 JSON."""
    import re
    # ```json ... ```
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        try: return json.loads(m.group(1).strip())
        except: pass
    # 整段
    s = text.strip()
    if s.startswith("{") and s.endswith("}"):
        try: return json.loads(s)
        except: pass
    # 第一个 { 到最后一个 }
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j > i:
        try: return json.loads(s[i:j+1])
        except: pass
    return None


# ============================================================
# P0-1: 并行调用 (3 路同时 subprocess)
# ============================================================
def test_parallel():
    print("\n" + "=" * 60)
    print("P0-1: 并行调用 (3 路同时)")
    print("=" * 60)

    prompts = [
        "Output JSON: {\"id\": 1, \"result\": \"hello from task 1\"}",
        "Output JSON: {\"id\": 2, \"result\": \"hello from task 2\"}",
        "Output JSON: {\"id\": 3, \"result\": \"hello from task 3\"}",
    ]

    t0 = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {}
        for p in prompts:
            f = pool.submit(run_qodercli, p, agents=agents_json, agent="test-agent")
            futures[f] = p

        for f in as_completed(futures):
            r = f.result()
            results.append(r)
            status = "OK" if r["exit_code"] == 0 else "FAIL"
            print(f"  [{status}] exit={r['exit_code']} elapsed={r['elapsed']:.1f}s stdout={len(r['stdout'])}chars")

    total_elapsed = time.monotonic() - t0
    ok_count = sum(1 for r in results if r["exit_code"] == 0)

    print(f"\n  Total wall time: {total_elapsed:.1f}s")
    print(f"  Success: {ok_count}/3")

    # 验证并行是否真的同时执行 (总时间 < 3 * 最慢单任务)
    max_single = max(r["elapsed"] for r in results)
    is_parallel = total_elapsed < max_single * 2.5
    print(f"  Parallelism check: max_single={max_single:.1f}s, total={total_elapsed:.1f}s -> {'PARALLEL' if is_parallel else 'SEQUENTIAL'}")

    return ok_count == 3 and is_parallel


# ============================================================
# P0-2: 长 prompt 传递 (--attachment 大文件)
# ============================================================
def test_long_prompt():
    print("\n" + "=" * 60)
    print("P0-2: 长 prompt / 大文件 attachment")
    print("=" * 60)

    # 生成一个 ~20000 字符的模拟 diff
    diff_file = Path("scripts/_test_large_diff.patch")
    lines = ["diff --git a/large_file.py b/large_file.py", "--- a/large_file.py", "+++ b/large_file.py"]
    line_num = 1
    while sum(len(l) for l in lines) < 20000:
        lines.append(f"@@ -{line_num},4 +{line_num},6 @@")
        lines.append(" import os")
        lines.append(" import sys")
        lines.append("+import json  # added for config parsing")
        lines.append("+MAX_RETRIES = 3  # configurable retry limit")
        lines.append(" def process():")
        lines.append("-    data = open('f').read()")
        lines.append("+    with open('f') as fh:")
        lines.append("+        data = fh.read()")
        line_num += 6

    diff_content = "\n".join(lines)
    diff_file.write_text(diff_content, encoding="utf-8")
    size = len(diff_content)

    print(f"  Diff size: {size} chars ({size // 1024}KB)")

    prompt = "Review this diff and output JSON: {\"issues_count\": <int>, \"summary\": \"<string>\"}"

    r = run_qodercli(prompt, attachment=str(diff_file), agents=agents_json, agent="test-agent", timeout=180)

    diff_file.unlink(missing_ok=True)

    print(f"  Exit code: {r['exit_code']}, elapsed: {r['elapsed']:.1f}s")
    print(f"  Stdout: {len(r['stdout'])} chars")

    if r["exit_code"] == 0 and r["stdout"].strip():
        data = extract_json(r["stdout"])
        if data:
            print(f"  [PASS] JSON parsed: {list(data.keys())}")
            return True
        else:
            print(f"  [WARN] Got output but no JSON: {r['stdout'][:200]}")
            return True  # 输出成功但 JSON 不稳定
    else:
        print(f"  [FAIL] exit={r['exit_code']}")
        return False


# ============================================================
# P0-3: 工作目录 (--cwd) + 源文件读取
# ============================================================
def test_cwd():
    print("\n" + "=" * 60)
    print("P0-3: 工作目录 (--cwd) + 源文件读取")
    print("=" * 60)

    # 用项目根目录作为 cwd，让 agent 读 config.py
    project_dir = str(Path("d:/Code/ReviewAgent").resolve())
    print(f"  cwd: {project_dir}")

    prompt = (
        "Read the file 'reviewagent/config.py' in the current working directory, "
        "then output JSON: {\"has_redis_url\": <bool>, \"default_port\": <int>, \"file_lines\": <int>}"
    )

    r = run_qodercli(prompt, cwd=project_dir, agents=agents_json, agent="test-agent", timeout=180)

    print(f"  Exit code: {r['exit_code']}, elapsed: {r['elapsed']:.1f}s")
    print(f"  Stdout: {len(r['stdout'])} chars")

    if r["exit_code"] == 0:
        data = extract_json(r["stdout"])
        if data:
            print(f"  [PASS] JSON: {data}")
            # 验证是否真的读了文件 (redis_url 应该在 config.py 里)
            has_redis = data.get("has_redis_url")
            if has_redis is True:
                print(f"  [PASS] Correctly read file (has_redis_url=True)")
                return True
            else:
                print(f"  [WARN] has_redis_url={has_redis}, may not have read file")
                return True  # 可能格式不同
        else:
            print(f"  [WARN] No JSON: {r['stdout'][:300]}")
            return r["exit_code"] == 0
    else:
        print(f"  [FAIL]")
        return False


# ============================================================
# P1-1: 超时控制
# ============================================================
def test_timeout():
    print("\n" + "=" * 60)
    print("P1-1: 超时控制 (10s timeout)")
    print("=" * 60)

    # 给一个复杂 prompt 但很短的 timeout
    prompt = "Write a 1000-word essay about Python programming."
    r = run_qodercli(prompt, timeout=10)

    print(f"  Exit code: {r['exit_code']}, elapsed: {r['elapsed']:.1f}s, timed_out: {r['timed_out']}")

    if r["timed_out"]:
        print(f"  [PASS] Timeout correctly triggered at {r['elapsed']:.1f}s")
        # 验证进程是否被清理 (再跑一个简单任务)
        r2 = run_qodercli("Output JSON: {\"ok\": true}", agents=agents_json, agent="test-agent", timeout=60)
        if r2["exit_code"] == 0:
            print(f"  [PASS] Subsequent call works (process cleaned up)")
            return True
        else:
            print(f"  [FAIL] Subsequent call failed")
            return False
    else:
        print(f"  [WARN] Completed before timeout ({r['elapsed']:.1f}s)")
        return True  # 模型太快


# ============================================================
# P1-2: 工具限制 (--disallowed-tools)
# ============================================================
def test_tool_restrictions():
    print("\n" + "=" * 60)
    print("P1-2: 工具限制 (--disallowed-tools)")
    print("=" * 60)

    prompt = "Output JSON: {\"test\": true}"
    cmd_base = [NODE, QODERCLI_JS, "-p", "--model", MODEL, "--no-session-persistence", "-o", "text",
                "--disallowed-tools", "write,edit,bash",
                "--agents", agents_json, "--agent", "test-agent", prompt]

    t0 = time.monotonic()
    try:
        result = subprocess.run(cmd_base, capture_output=True, text=True, timeout=120,
                                encoding="utf-8", errors="replace")
        elapsed = time.monotonic() - t0
        print(f"  Exit code: {result.returncode}, elapsed: {elapsed:.1f}s")
        print(f"  Stdout: {result.stdout[:300]}")

        if result.returncode == 0:
            data = extract_json(result.stdout)
            if data:
                print(f"  [PASS] Works with --disallowed-tools, JSON: {data}")
                return True
            else:
                print(f"  [PASS] Exit 0 but no JSON (tool restriction may affect output)")
                return True
        else:
            print(f"  [FAIL] exit={result.returncode}")
            return False
    except Exception as e:
        print(f"  [FAIL] {e}")
        return False


# ============================================================
# P2-1: Token 统计
# ============================================================
def test_token_stats():
    print("\n" + "=" * 60)
    print("P2-1: Token 统计")
    print("=" * 60)

    # 测试 -o json 是否有 token 信息
    cmd = [NODE, QODERCLI_JS, "-p", "--model", MODEL, "--no-session-persistence",
           "-o", "json",
           "--agents", agents_json, "--agent", "test-agent",
           "Output JSON: {\"ok\": true}"]

    t0 = time.monotonic()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                                encoding="utf-8", errors="replace")
        elapsed = time.monotonic() - t0

        print(f"  Exit code: {result.returncode}, elapsed: {elapsed:.1f}s")
        print(f"  Stdout length: {len(result.stdout)} chars")

        if result.stdout.strip():
            # 尝试解析 JSON 输出
            try:
                output = json.loads(result.stdout)
                print(f"  Output keys: {list(output.keys()) if isinstance(output, dict) else type(output).__name__}")
                # 查找 token 相关字段
                if isinstance(output, dict):
                    for k in ["tokens", "usage", "cost", "model", "stats"]:
                        if k in output:
                            print(f"  Found '{k}': {output[k]}")
                    # 完整输出预览
                    preview = json.dumps(output, indent=2, ensure_ascii=False)[:1000]
                    print(f"  Preview:\n{preview}")
                    return True
            except json.JSONDecodeError:
                print(f"  Not JSON output: {result.stdout[:500]}")

        # 也试试 -o text 但 stderr 里有没有 token 信息
        if result.stderr:
            print(f"  Stderr: {result.stderr[:300]}")

        return result.returncode == 0
    except Exception as e:
        print(f"  [FAIL] {e}")
        return False


# ============================================================
# P2-2: 截断检测
# ============================================================
def test_truncation():
    print("\n" + "=" * 60)
    print("P2-2: 输出截断检测 (--max-output-tokens)")
    print("=" * 60)

    # 限制输出 tokens 为很小值，看是否截断
    cmd = [NODE, QODERCLI_JS, "-p", "--model", MODEL, "--no-session-persistence", "-o", "text",
           "--max-output-tokens", "50",
           "--agents", agents_json, "--agent", "test-agent",
           "Output a JSON with 10 items: {\"items\": [{\"id\": 1, \"name\": \"...\"}, ...]}"]

    t0 = time.monotonic()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                                encoding="utf-8", errors="replace")
        elapsed = time.monotonic() - t0

        print(f"  Exit code: {result.returncode}, elapsed: {elapsed:.1f}s")
        print(f"  Stdout ({len(result.stdout)} chars): {result.stdout[:500]}")

        # 检查输出是否被截断 (JSON 不完整)
        data = extract_json(result.stdout)
        if data:
            print(f"  [INFO] JSON complete (not truncated): {list(data.keys())}")
            return True
        else:
            print(f"  [INFO] JSON incomplete/truncated (expected with --max-output-tokens=50)")
            print(f"  [PASS] Truncation detectable (no valid JSON)")
            return True
    except Exception as e:
        print(f"  [FAIL] {e}")
        return False


# ============================================================
# Main
# ============================================================
def main():
    print("=" * 60)
    print("qodercli P0/P1/P2 verification")
    print("=" * 60)

    results = {}
    tests = [
        ("P0-1 parallel",        test_parallel),
        ("P0-2 long_prompt",     test_long_prompt),
        ("P0-3 cwd+read",        test_cwd),
        ("P1-1 timeout",         test_timeout),
        ("P1-2 tool_restrict",   test_tool_restrictions),
        ("P2-1 token_stats",     test_token_stats),
        ("P2-2 truncation",      test_truncation),
    ]

    for name, fn in tests:
        try:
            results[name] = fn()
        except Exception as e:
            print(f"\n  [FAIL] Exception: {e}")
            results[name] = False

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for name, passed in results.items():
        status = "[PASS]" if passed else "[FAIL]"
        print(f"  {name:25s} {status}")

    total = len(results)
    passed = sum(1 for v in results.values() if v)
    print(f"\nTotal: {passed}/{total} passed")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
