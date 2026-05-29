"""LLM client abstraction: mock (no key), OpenAI API, or custom base URL."""
import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Any

import config

logger = logging.getLogger(__name__)


def _default_system_prompt() -> str:
    return """你是一位面向球迷的足球比赛分析写手。根据提供的结构化数据（比分、统计、关键时刻），用简洁、有洞察的中文写一份比赛复盘报告。
报告应包含：比赛概况、双方表现对比、关键瞬间、谁主导比赛、简要结论。语气友好，适合普通球迷阅读。
若数据中的 data_source 标明为「仅比分与进球时刻」，请在报告中简要注明本场数据来源及范围，不要编造传球、控球等未提供的统计。"""


def _build_user_message(payload: dict[str, Any]) -> str:
    return f"""请根据以下比赛数据生成一份球迷向复盘报告（Markdown 格式）。

## 数据
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""


class BaseLLMClient(ABC):
    @abstractmethod
    def generate(self, payload: dict[str, Any], system_prompt: str | None = None, user_message: str | None = None) -> str:
        """Return generated report text (Markdown)."""
        pass


class MockLLMClient(BaseLLMClient):
    """Returns a fixed template report for testing without API key."""

    def generate(self, payload: dict[str, Any], system_prompt: str | None = None, user_message: str | None = None) -> str:
        title = payload.get("title", "比赛")
        score = payload.get("score", "0-0")
        half_time = payload.get("half_time_score")
        home = payload.get("home_team_name", "主队")
        away = payload.get("away_team_name", "客队")
        comp = payload.get("competition_name", "")
        season = payload.get("season_name", "")
        moments = payload.get("tactical", {}).get("key_moments", [])[:5]
        moments_text = "\n".join(f"- {m}" for m in moments) if moments else "（暂无关键时刻列表）"
        home_stats = payload.get("stats", {}).get("home", {})
        away_stats = payload.get("stats", {}).get("away", {})
        score_line = f"**比分** {score} | **赛事** {comp} {season}"
        if half_time is not None:
            score_line += f" | **半场** {half_time}"
        return f"""# {title} 复盘报告（Mock）

{score_line}

## 比赛概况
{home} 与 {away} 的较量，最终比分 {score}。本报告由系统根据事件数据自动生成（Mock 模式，未调用真实 LLM）。

## 关键瞬间
{moments_text}

## 数据摘要
- 主队控球约 {home_stats.get('possession_pct', 0)}%，传球 {home_stats.get('passes', 0)} 次，射门 {home_stats.get('shots', 0)} 次。
- 客队控球约 {away_stats.get('possession_pct', 0)}%，传球 {away_stats.get('passes', 0)} 次，射门 {away_stats.get('shots', 0)} 次。

## 小结
以上为基于结构化数据的模板输出。配置 LLM_API_KEY 后可生成真实 AI 复盘。
"""


class OpenAILikeClient(BaseLLMClient):
    """OpenAI-compatible API (OpenAI or custom base URL)."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None):
        self.api_key = api_key or config.LLM_API_KEY or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url or config.LLM_BASE_URL or None
        self.model = model or config.LLM_MODEL

    def generate(self, payload: dict[str, Any], system_prompt: str | None = None, user_message: str | None = None) -> str:
        if not self.api_key and not self.base_url:
            return MockLLMClient().generate(payload, system_prompt, user_message)
        try:
            from openai import OpenAI
        except ImportError:
            logger.warning("未安装 openai 包，无法调用 API，使用 Mock 报告。请执行: pip install openai")
            return MockLLMClient().generate(payload, system_prompt, user_message)
        client = OpenAI(api_key=self.api_key, base_url=self.base_url or None)
        sys = system_prompt or _default_system_prompt()
        user = user_message or _build_user_message(payload)
        resp = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": sys},
                {"role": "user", "content": user},
            ],
        )
        return (resp.choices[0].message.content or "").strip()


def get_llm_client() -> BaseLLMClient:
    provider = (config.LLM_PROVIDER or os.getenv("LLM_PROVIDER", "mock")).lower()
    if provider == "openai" and (config.LLM_API_KEY or os.getenv("OPENAI_API_KEY")):
        return OpenAILikeClient()
    return MockLLMClient()
