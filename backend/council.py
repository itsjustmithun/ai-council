"""3-stage AI Council orchestration."""


from typing import List, Dict, Any, Tuple, AsyncGenerator
import asyncio
from .openrouter import query_model, query_model_stream
from .config import COUNCIL_MODELS, CHAIRMAN_MODEL





async def stage1_collect_responses_stream(user_query: str) -> AsyncGenerator[Tuple[str, str], None]:
    """
    Stage 1: Stream individual responses from all council models.
    Yields (model_name, chunk_content) tuples.
    """
    messages = [{"role": "user", "content": user_query}]
    queue = asyncio.Queue()
    active_models = len(COUNCIL_MODELS)

    async def stream_worker(model_name: str):
        try:
            async for chunk in query_model_stream(model_name, messages):
                if chunk:
                    await queue.put((model_name, chunk))
        except Exception:
            pass  # Fail gracefully
        finally:
            await queue.put((model_name, None))  # Signal done

    # Start workers
    for model in COUNCIL_MODELS:
        asyncio.create_task(stream_worker(model))

    # Consumer loop
    completed_models = 0
    while completed_models < active_models:
        item = await queue.get()
        model, chunk = item
        
        if chunk is None:
            completed_models += 1
        else:
            yield model, chunk





async def stage2_collect_rankings_stream(
    user_query: str,
    stage1_results: List[Dict[str, Any]]
) -> AsyncGenerator[Tuple[str, str, Dict[str, str]], None]:
    """
    Stage 2: Stream rankings from council models.
    Yields (model_name, chunk_content, label_to_model_mapping) tuples.
    Note: mapping is yielded once at start (with None content), then chunks.
    """
    # Create anonymized labels and prompt (reuse logic)
    labels = [chr(65 + i) for i in range(len(stage1_results))]
    label_to_model = {
        f"Response {label}": result['model']
        for label, result in zip(labels, stage1_results)
    }
    
    responses_text = "\n\n".join([
        f"Response {label}:\n{result['response']}"
        for label, result in zip(labels, stage1_results)
    ])

    ranking_prompt = f"""You are evaluating different responses to the following question:

Question: {user_query}

Here are the responses from different models (anonymized):

{responses_text}

Your task:
1. First, evaluate each response individually. For each response, explain what it does well and what it does poorly.
2. Then, at the very end of your response, provide a final ranking.

IMPORTANT: Your final ranking MUST be formatted EXACTLY as follows:
- Start with the line "FINAL RANKING:" (all caps, with colon)
- Then list the responses from best to worst as a numbered list
- Each line should be: number, period, space, then ONLY the response label (e.g., "1. Response A")
- Do not add any other text or explanations in the ranking section

Example of the correct format for your ENTIRE response:

Response A provides good detail on X but misses Y...
Response B is accurate but lacks depth on Z...
Response C offers the most comprehensive answer...

FINAL RANKING:
1. Response C
2. Response A
3. Response B

Now provide your evaluation and ranking:"""

    messages = [{"role": "user", "content": ranking_prompt}]
    
    # Yield mapping first
    yield None, None, label_to_model
    
    queue = asyncio.Queue()
    active_models = len(COUNCIL_MODELS)

    async def stream_worker(model_name: str):
        try:
            async for chunk in query_model_stream(model_name, messages):
                if chunk:
                    await queue.put((model_name, chunk))
        except Exception:
            pass
        finally:
            await queue.put((model_name, None))

    for model in COUNCIL_MODELS:
        asyncio.create_task(stream_worker(model))

    completed_models = 0
    while completed_models < active_models:
        item = await queue.get()
        model, chunk = item
        
        if chunk is None:
            completed_models += 1
        else:
            yield model, chunk, label_to_model





async def stage3_synthesize_final_stream(
    user_query: str,
    stage1_results: List[Dict[str, Any]],
    stage2_results: List[Dict[str, Any]]
) -> AsyncGenerator[str, None]:
    """
    Stage 3: Stream Chairman's final synthesis.
    Yields chunks of text content.
    """
    stage1_text = "\n\n".join([
        f"Model: {result['model']}\nResponse: {result['response']}"
        for result in stage1_results
    ])

    stage2_text = "\n\n".join([
        f"Model: {result['model']}\nRanking: {result['ranking']}"
        for result in stage2_results
    ])

    chairman_prompt = f"""You are the Chairman of an AI Council. Multiple AI models have provided responses to a user's question, and then ranked each other's responses.

Original Question: {user_query}

STAGE 1 - Individual Responses:
{stage1_text}

STAGE 2 - Peer Rankings:
{stage2_text}

Your task as Chairman is to synthesize all of this information into a single, comprehensive, accurate answer to the user's original question. Consider:
- The individual responses and their insights
- The peer rankings and what they reveal about response quality
- Any patterns of agreement or disagreement

Provide a clear, well-reasoned final answer that represents the council's collective wisdom:"""

    messages = [{"role": "user", "content": chairman_prompt}]

    async for chunk in query_model_stream(CHAIRMAN_MODEL, messages):
        if chunk:
            yield chunk


def parse_ranking_from_text(ranking_text: str) -> List[str]:
    """
    Parse the FINAL RANKING section from the model's response.

    Args:
        ranking_text: The full text response from the model

    Returns:
        List of response labels in ranked order
    """
    import re

    # Look for "FINAL RANKING:" section
    if "FINAL RANKING:" in ranking_text:
        # Extract everything after "FINAL RANKING:"
        parts = ranking_text.split("FINAL RANKING:")
        if len(parts) >= 2:
            ranking_section = parts[1]
            # Try to extract numbered list format (e.g., "1. Response A")
            # This pattern looks for: number, period, optional space, "Response X"
            numbered_matches = re.findall(r'\d+\.\s*Response [A-Z]', ranking_section)
            if numbered_matches:
                # Extract just the "Response X" part
                return [re.search(r'Response [A-Z]', m).group() for m in numbered_matches]

            # Fallback: Extract all "Response X" patterns in order
            matches = re.findall(r'Response [A-Z]', ranking_section)
            return matches

    # Fallback: try to find any "Response X" patterns in order
    matches = re.findall(r'Response [A-Z]', ranking_text)
    return matches


def calculate_aggregate_rankings(
    stage2_results: List[Dict[str, Any]],
    label_to_model: Dict[str, str]
) -> List[Dict[str, Any]]:
    """
    Calculate aggregate rankings across all models.

    Args:
        stage2_results: Rankings from each model
        label_to_model: Mapping from anonymous labels to model names

    Returns:
        List of dicts with model name and average rank, sorted best to worst
    """
    from collections import defaultdict

    # Track positions for each model
    model_positions = defaultdict(list)

    for ranking in stage2_results:
        ranking_text = ranking['ranking']

        # Parse the ranking from the structured format
        parsed_ranking = parse_ranking_from_text(ranking_text)

        for position, label in enumerate(parsed_ranking, start=1):
            if label in label_to_model:
                model_name = label_to_model[label]
                model_positions[model_name].append(position)

    # Calculate average position for each model
    aggregate = []
    for model, positions in model_positions.items():
        if positions:
            avg_rank = sum(positions) / len(positions)
            aggregate.append({
                "model": model,
                "average_rank": round(avg_rank, 2),
                "rankings_count": len(positions)
            })

    # Sort by average rank (lower is better)
    aggregate.sort(key=lambda x: x['average_rank'])

    return aggregate


async def generate_conversation_title(user_query: str) -> str:
    """
    Generate a short title for a conversation based on the first user message.

    Args:
        user_query: The first user message

    Returns:
        A short title (3-5 words)
    """
    title_prompt = f"""Generate a very short title (3-5 words maximum) that summarizes the following question.
The title should be concise and descriptive. Do not use quotes or punctuation in the title.

Question: {user_query}

Title:"""

    messages = [{"role": "user", "content": title_prompt}]

    # Use gemini-2.5-flash for title generation (fast and cheap)
    response = await query_model("google/gemini-2.5-flash", messages, timeout=30.0)

    if response is None:
        # Fallback to a generic title
        return "New Conversation"

    title = response.get('content', 'New Conversation').strip()

    # Clean up the title - remove quotes, limit length
    title = title.strip('"\'')

    # Truncate if too long
    if len(title) > 50:
        title = title[:47] + "..."

    return title



