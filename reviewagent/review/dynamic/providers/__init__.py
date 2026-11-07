"""动态证据 provider 注册（导入即注册，启动装入后即可按 id 取用）."""
from __future__ import annotations

from reviewagent.review.dynamic.providers.cross_file_refs import CrossFileRefsProvider
from reviewagent.review.dynamic.registry import register

register("cross_file_refs", lambda *, fetch_callback=None, **kw: CrossFileRefsProvider(fetch_callback))
