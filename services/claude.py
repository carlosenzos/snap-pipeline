from __future__ import annotations

import asyncio
import logging
import time

import anthropic

from config.settings import get_settings

logger = logging.getLogger(__name__)

API_MAX_RETRIES = 3
API_RETRY_BASE_DELAY = 30

# Claude Opus 4 pricing (per million tokens)
INPUT_COST_PER_M = 15.0
OUTPUT_COST_PER_M = 75.0


def _format_search_results(block) -> str:
    """Extract titles + URLs from a web_search_tool_result block."""
    lines = []
    content = getattr(block, "content", None)
    if isinstance(content, list):
        for item in content:
            title = getattr(item, "title", "")
            url = getattr(item, "url", "")
            if title or url:
                lines.append(f"  - {title} | {url}")
    return "\n".join(lines) if lines else "  [results received]"


async def write_script(
    system_prompt: str,
    topic: str,
    instructions: str = "",
    articles: list[dict] | None = None,
    image_urls: list[dict] | None = None,
) -> dict:
    """Use Claude to write a Snap script with web search and research context.

    Returns:
        Dict with "script", "stats", "research", and "search_urls".
    """
    s = get_settings()
    client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key)

    full_system = system_prompt

    if instructions:
        full_system += f"\n\n--- ADDITIONAL INSTRUCTIONS FROM PRODUCER ---\n{instructions}"

    full_system += (
        "\n\nIMPORTANT: You MUST always write the full script. Never refuse, never "
        "ask for clarification, never say you need more information. Use your web "
        "search tool to research the topic and get the latest facts. Use any "
        "reference articles provided below as primary sources. Your output should "
        "ONLY be the script text — no meta-commentary, no questions, no disclaimers."
    )

    user_blocks = [{"type": "text", "text": f"Write the full script for this Snap: {topic}"}]

    if articles:
        articles_text = "\n\n--- REFERENCE ARTICLES (provided by producer) ---"
        for i, article in enumerate(articles, 1):
            articles_text += f"\n\n[{i}] {article['url']}\n{article['content']}"
        user_blocks.append({"type": "text", "text": articles_text})

    if image_urls:
        user_blocks.append({
            "type": "text",
            "text": "\n\n--- ATTACHED IMAGES (from producer) ---\nUse these images as visual reference for your script:",
        })
        for img in image_urls:
            user_blocks.append({
                "type": "image",
                "source": {"type": "url", "url": img["url"]},
            })
            if img.get("name"):
                user_blocks.append({"type": "text", "text": f"Image: {img['name']}"})

    start = time.time()
    message = None

    for attempt in range(API_MAX_RETRIES + 1):
        try:
            async with client.messages.stream(
                model="claude-opus-4-6",
                max_tokens=40000,
                thinking={
                    "type": "adaptive",
                },
                tools=[{
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": 5,
                }],
                system=full_system,
                messages=[{"role": "user", "content": user_blocks}],
            ) as stream:
                message = await stream.get_final_message()
            break
        except (anthropic.APIStatusError, anthropic.AnthropicError) as e:
            err_str = str(e).lower()
            is_retryable = "overloaded" in err_str or "529" in err_str or "rate" in err_str
            if is_retryable and attempt < API_MAX_RETRIES:
                wait = API_RETRY_BASE_DELAY * (attempt + 1)
                logger.warning(
                    "Claude API error (attempt %d/%d): %s — retrying in %ds...",
                    attempt + 1, API_MAX_RETRIES, e, wait,
                )
                await asyncio.sleep(wait)
            else:
                raise

    elapsed = time.time() - start

    script = ""
    research_parts = []
    search_urls = []

    for block in message.content:
        if block.type == "thinking":
            research_parts.append(f"[Thinking]\n{block.thinking}")

        elif block.type == "server_tool_use" and getattr(block, "name", "") == "web_search":
            query = block.input.get("query", "") if hasattr(block, "input") else ""
            research_parts.append(f'[Web Search] "{query}"')

        elif block.type == "web_search_tool_result":
            results_text = _format_search_results(block)
            research_parts.append(f"[Search Results]\n{results_text}")
            content = getattr(block, "content", None)
            if isinstance(content, list):
                for item in content:
                    url = getattr(item, "url", "")
                    title = getattr(item, "title", "")
                    if url:
                        search_urls.append({"url": url, "title": title})

        elif block.type == "text":
            script += block.text

    research_log = "=== RESEARCH LOG ===\n\n"

    if instructions:
        research_log += f"--- INSTRUCTIONS FROM CARD ---\n{instructions}\n\n"

    if articles:
        research_log += "--- FETCHED ARTICLES ---\n"
        for i, article in enumerate(articles, 1):
            preview = article["content"][:200] + "..."
            research_log += f"[{i}] {article['url']}\n    {preview}\n"
        research_log += "\n"

    research_log += "--- CLAUDE'S RESEARCH PROCESS ---\n\n"
    research_log += "\n\n".join(research_parts)

    word_count = len(script.split())
    if word_count < 50:
        logger.warning("Script too short (%d words), likely a refusal or error: %s", word_count, script[:300])
        raise ValueError(
            f"Claude produced only {word_count} words (minimum 50). "
            f"Output: {script[:300]}..."
        )

    input_tokens = message.usage.input_tokens
    output_tokens = message.usage.output_tokens
    cost = (input_tokens * INPUT_COST_PER_M + output_tokens * OUTPUT_COST_PER_M) / 1_000_000

    stats = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": round(cost, 2),
        "duration_s": round(elapsed, 1),
        "char_count": len(script),
        "word_count": word_count,
    }

    logger.info(
        "Script: %d words, %d chars | Tokens: %d in / %d out | Cost: $%.2f | Time: %.1fs",
        word_count, len(script), input_tokens, output_tokens, cost, elapsed,
    )
    return {"script": script, "stats": stats, "research": research_log, "search_urls": search_urls}


# Sonnet 4.5 pricing (per million tokens)
SONNET_INPUT_COST_PER_M = 3.0
SONNET_OUTPUT_COST_PER_M = 15.0


async def revise_script(
    system_prompt: str,
    current_script: str,
    revision_prompt: str,
) -> dict:
    """Revise an existing script based on producer feedback.

    Uses Sonnet 4.5 (cheaper than Opus, plenty good for edits).
    """
    s = get_settings()
    client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key)

    full_system = system_prompt + (
        "\n\nYou are revising an existing Snap script based on producer feedback. "
        "Apply the requested changes while maintaining the overall style, tone, and format. "
        "Output ONLY the complete revised script — no explanations, no meta-commentary."
    )

    start = time.time()
    message = None

    for attempt in range(API_MAX_RETRIES + 1):
        try:
            async with client.messages.stream(
                model="claude-sonnet-4-5-20250929",
                max_tokens=40000,
                thinking={
                    "type": "adaptive",
                },
                system=full_system,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Here is the current script:\n\n{current_script}\n\n"
                        f"--- REVISION REQUESTED ---\n{revision_prompt}"
                    ),
                }],
            ) as stream:
                message = await stream.get_final_message()
            break
        except (anthropic.APIStatusError, anthropic.AnthropicError) as e:
            err_str = str(e).lower()
            is_retryable = "overloaded" in err_str or "529" in err_str or "rate" in err_str
            if is_retryable and attempt < API_MAX_RETRIES:
                wait = API_RETRY_BASE_DELAY * (attempt + 1)
                logger.warning(
                    "Claude API error (attempt %d/%d): %s — retrying in %ds...",
                    attempt + 1, API_MAX_RETRIES, e, wait,
                )
                await asyncio.sleep(wait)
            else:
                raise

    elapsed = time.time() - start

    script = ""
    for block in message.content:
        if block.type == "text":
            script += block.text

    _REFUSAL_PHRASES = [
        "i can't revise",
        "i cannot create",
        "i can't create",
        "i appreciate you sharing",
        "crosses an ethical line",
        "deliberately deceives",
        "i'm not able to",
        "i cannot write",
        "i can't write",
        "i need to decline",
        "i must decline",
        "misleading clickbait",
        "i cannot help with",
        "i can't help with",
    ]
    script_lower = script.lower()
    for phrase in _REFUSAL_PHRASES:
        if phrase in script_lower:
            logger.warning("Claude refused to revise script (matched: %r): %s", phrase, script[:200])
            raise ValueError(
                f"Claude refused to revise the script: {script[:300]}..."
            )

    input_tokens = message.usage.input_tokens
    output_tokens = message.usage.output_tokens
    cost = (input_tokens * SONNET_INPUT_COST_PER_M + output_tokens * SONNET_OUTPUT_COST_PER_M) / 1_000_000
    word_count = len(script.split())

    stats = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": round(cost, 4),
        "duration_s": round(elapsed, 1),
        "word_count": word_count,
    }

    logger.info(
        "Revision: %d words | Tokens: %d in / %d out | $%.4f | %.1fs",
        word_count, input_tokens, output_tokens, cost, elapsed,
    )
    return {"script": script, "stats": stats}
