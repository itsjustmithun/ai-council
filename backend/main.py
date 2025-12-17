"""FastAPI backend for AI Council."""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Dict, Any
import uuid
import json
import asyncio

from . import storage
from .council import (
    generate_conversation_title,
    stage1_collect_responses_stream,
    stage2_collect_rankings_stream,
    stage3_synthesize_final_stream,
    parse_ranking_from_text
)

app = FastAPI(title="AI Council API")

# Enable CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class CreateConversationRequest(BaseModel):
    """Request to create a new conversation."""
    pass


class SendMessageRequest(BaseModel):
    """Request to send a message in a conversation."""
    content: str


class ConversationMetadata(BaseModel):
    """Conversation metadata for list view."""
    id: str
    created_at: str
    title: str
    message_count: int


class Conversation(BaseModel):
    """Full conversation with all messages."""
    id: str
    created_at: str
    title: str
    messages: List[Dict[str, Any]]


@app.get("/")
async def root():
    """Health check endpoint."""
    return {"status": "ok", "service": "AI Council API"}


@app.get("/api/conversations", response_model=List[ConversationMetadata])
async def list_conversations():
    """List all conversations (metadata only)."""
    return storage.list_conversations()


@app.post("/api/conversations", response_model=Conversation)
async def create_conversation(request: CreateConversationRequest):
    """Create a new conversation."""
    conversation_id = str(uuid.uuid4())
    conversation = storage.create_conversation(conversation_id)
    return conversation


@app.get("/api/conversations/{conversation_id}", response_model=Conversation)
async def get_conversation(conversation_id: str):
    """Get a specific conversation with all its messages."""
    conversation = storage.get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation





@app.post("/api/conversations/{conversation_id}/message/stream")
async def send_message_stream(conversation_id: str, request: SendMessageRequest):
    """
    Send a message and stream the 3-stage council process using SSE.
    """
    # Check if conversation exists
    conversation = storage.get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Check if this is the first message
    is_first_message = len(conversation["messages"]) == 0

    async def event_generator():
        try:
            # Add user message
            storage.add_user_message(conversation_id, request.content)

            # Start title generation in parallel (don't await yet)
            title_task = None
            if is_first_message:
                title_task = asyncio.create_task(generate_conversation_title(request.content))

            # --- Stage 1: Collect responses ---
            yield f"data: {json.dumps({'type': 'stage1_start'})}\n\n"
            
            stage1_accumulators = {} # model -> content
            
            async for model, chunk in stage1_collect_responses_stream(request.content):
                if model not in stage1_accumulators:
                    stage1_accumulators[model] = ""
                stage1_accumulators[model] += chunk
                
                event = {
                    "type": "stage1_chunk",
                    "model": model, 
                    "content": chunk
                }
                yield f"data: {json.dumps(event)}\n\n"
            
            # Reconstruct stage1_results format for next stage
            stage1_results = []
            for model, content in stage1_accumulators.items():
                stage1_results.append({
                    "model": model,
                    "response": content
                })
                
            yield f"data: {json.dumps({'type': 'stage1_complete', 'data': stage1_results})}\n\n"

            # --- Stage 2: Collect rankings ---
            yield f"data: {json.dumps({'type': 'stage2_start'})}\n\n"
            
            stage2_accumulators = {} # model -> content
            label_to_model = {}
            
            async for model, chunk, mapping in stage2_collect_rankings_stream(request.content, stage1_results):
                if model is None and mapping:
                    label_to_model = mapping
                    yield f"data: {json.dumps({'type': 'stage2_metadata', 'metadata': {'label_to_model': label_to_model}})}\n\n"
                    continue
                    
                if model not in stage2_accumulators:
                    stage2_accumulators[model] = ""
                stage2_accumulators[model] += chunk
                
                event = {
                    "type": "stage2_chunk",
                    "model": model,
                    "content": chunk
                }
                yield f"data: {json.dumps(event)}\n\n"

            # Reconstruct stage2_results and calculate aggregate
            stage2_results = []
            for model, content in stage2_accumulators.items():
                parsed = parse_ranking_from_text(content)
                stage2_results.append({
                    "model": model,
                    "ranking": content,
                    "parsed_ranking": parsed
                })
                
            aggregate_rankings = calculate_aggregate_rankings(stage2_results, label_to_model)
            
            yield f"data: {json.dumps({'type': 'stage2_complete', 'data': stage2_results, 'metadata': {'label_to_model': label_to_model, 'aggregate_rankings': aggregate_rankings}})}\n\n"

            # --- Stage 3: Synthesize final answer ---
            yield f"data: {json.dumps({'type': 'stage3_start'})}\n\n"
            
            stage3_content = ""
            async for chunk in stage3_synthesize_final_stream(request.content, stage1_results, stage2_results):
                stage3_content += chunk
                event = {
                    "type": "stage3_chunk",
                    "content": chunk
                }
                yield f"data: {json.dumps(event)}\n\n"

            from .config import CHAIRMAN_MODEL
            stage3_result = {
                "model": CHAIRMAN_MODEL,
                "response": stage3_content
            }
            yield f"data: {json.dumps({'type': 'stage3_complete', 'data': stage3_result})}\n\n"

            # Wait for title generation if it was started
            if title_task:
                title = await title_task
                storage.update_conversation_title(conversation_id, title)
                yield f"data: {json.dumps({'type': 'title_complete', 'data': {'title': title}})}\n\n"

            # Save complete assistant message
            storage.add_assistant_message(
                conversation_id,
                stage1_results,
                stage2_results,
                stage3_result
            )

            # Send completion event
            yield f"data: {json.dumps({'type': 'complete'})}\n\n"

        except Exception as e:
            # Send error event
            print(f"Streaming error: {e}")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no", # Critical for Nginx/Proxies to not buffer
        }
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
