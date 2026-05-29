"""Assemble report: payload -> LLM -> full report (title + body)."""
from typing import Any

from report.llm_client import get_llm_client


class ReportAssembler:
    """Takes report payload, calls LLM, returns full Markdown report."""

    def __init__(self, llm_client=None):
        self.llm = llm_client or get_llm_client()

    def generate(self, payload: dict[str, Any]) -> str:
        """Generate full report Markdown (title already in LLM output or we prepend)."""
        if not payload:
            return "# 无数据\n无法生成报告。"
        body = self.llm.generate(payload)
        if not body.strip().startswith("#"):
            title = payload.get("title", "比赛复盘")
            body = f"# {title}\n\n{body}"
        return body.strip()
