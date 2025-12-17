import { useState, useEffect } from 'react';
import Sidebar from './components/Sidebar';
import ChatInterface from './components/ChatInterface';
import { api } from './api';
import './App.css';

function App() {
  const [conversations, setConversations] = useState([]);
  const [currentConversationId, setCurrentConversationId] = useState(null);
  const [currentConversation, setCurrentConversation] = useState(null);
  const [isLoading, setIsLoading] = useState(false);

  // Load conversations on mount
  useEffect(() => {
    loadConversations();
  }, []);

  // Load conversation details when selected
  useEffect(() => {
    if (currentConversationId) {
      loadConversation(currentConversationId);
    }
  }, [currentConversationId]);

  const loadConversations = async () => {
    try {
      const convs = await api.listConversations();
      setConversations(convs);
    } catch (error) {
      console.error('Failed to load conversations:', error);
    }
  };

  const loadConversation = async (id) => {
    try {
      const conv = await api.getConversation(id);
      setCurrentConversation(conv);
    } catch (error) {
      console.error('Failed to load conversation:', error);
    }
  };

  const handleNewConversation = async () => {
    try {
      const newConv = await api.createConversation();
      setConversations([
        { id: newConv.id, created_at: newConv.created_at, message_count: 0 },
        ...conversations,
      ]);
      setCurrentConversationId(newConv.id);
    } catch (error) {
      console.error('Failed to create conversation:', error);
    }
  };

  const handleSelectConversation = (id) => {
    setCurrentConversationId(id);
  };

  const handleSendMessage = async (content) => {
    if (!currentConversationId) return;

    setIsLoading(true);
    try {
      // Optimistically add user message to UI
      const userMessage = { role: 'user', content };
      setCurrentConversation((prev) => ({
        ...prev,
        messages: [...prev.messages, userMessage],
      }));

      // Create a partial assistant message
      const assistantMessage = {
        role: 'assistant',
        stage1: null,
        stage2: null,
        stage3: null,
        metadata: null,
        loading: {
          stage1: false,
          stage2: false,
          stage3: false,
        },
      };

      // Add the partial assistant message
      setCurrentConversation((prev) => ({
        ...prev,
        messages: [...prev.messages, assistantMessage],
      }));

      // Send message with streaming
      await api.sendMessageStream(currentConversationId, content, (eventType, event) => {
        setCurrentConversation((prev) => {
          const messages = [...prev.messages];
          const lastMsgIndex = messages.length - 1;
          const lastMsg = { ...messages[lastMsgIndex] };

          // Helper to safely update loading state
          const updateLoading = (stage, isLoading) => {
            lastMsg.loading = { ...lastMsg.loading, [stage]: isLoading };
          };

          switch (eventType) {
            case 'stage1_start':
              lastMsg.stage1 = [];
              updateLoading('stage1', true);
              break;

            case 'stage1_chunk':
              // Append to specific model's response
              {
                const currentStage1 = [...(lastMsg.stage1 || [])];
                const modelIndex = currentStage1.findIndex(r => r.model === event.model);

                if (modelIndex >= 0) {
                  const updatedItem = { ...currentStage1[modelIndex] };
                  updatedItem.response += event.content;
                  currentStage1[modelIndex] = updatedItem;
                } else {
                  currentStage1.push({ model: event.model, response: event.content });
                }
                lastMsg.stage1 = currentStage1;
              }
              break;

            case 'stage1_complete':
              lastMsg.stage1 = event.data;
              updateLoading('stage1', false);
              break;

            case 'stage2_start':
              lastMsg.stage2 = [];
              updateLoading('stage2', true);
              break;

            case 'stage2_metadata':
              lastMsg.metadata = {
                ...(lastMsg.metadata || {}),
                label_to_model: event.metadata.label_to_model
              };
              break;

            case 'stage2_chunk':
              {
                const currentStage2 = [...(lastMsg.stage2 || [])];
                const modelIndex = currentStage2.findIndex(r => r.model === event.model);

                if (modelIndex >= 0) {
                  const updatedItem = { ...currentStage2[modelIndex] };
                  updatedItem.ranking += event.content;
                  // Note: parsed_ranking will be missing/stale until complete, which is expected
                  currentStage2[modelIndex] = updatedItem;
                } else {
                  currentStage2.push({ model: event.model, ranking: event.content });
                }
                lastMsg.stage2 = currentStage2;
              }
              break;

            case 'stage2_complete':
              lastMsg.stage2 = event.data;
              lastMsg.metadata = event.metadata;
              updateLoading('stage2', false);
              break;

            case 'stage3_start':
              // Initialize stage3 object if needed, though we usually wait for chunk
              lastMsg.stage3 = { model: '', response: '' };
              updateLoading('stage3', true);
              break;

            case 'stage3_chunk':
              {
                const currentStage3 = { ...(lastMsg.stage3 || { model: '', response: '' }) };
                // We don't get model name in chunks usually, but we know it's chairman
                // Or we could send it. For now, just update response.
                currentStage3.response += event.content;
                lastMsg.stage3 = currentStage3;
              }
              break;

            case 'stage3_complete':
              lastMsg.stage3 = event.data;
              updateLoading('stage3', false);
              break;

            case 'title_complete':
              // Reload conversations to get updated title
              // But we can't call loadConversations() here directly if it's not available in scope cleanly
              // or might cause race conditions. 
              // Actually loadConversations IS in scope (closure).
              loadConversations();
              break;

            case 'complete':
              loadConversations();
              setIsLoading(false);
              break;

            case 'error':
              console.error('Stream error:', event.message);
              setIsLoading(false);
              break;

            default:
              // console.log('Ignore', eventType);
              break;
          }

          messages[lastMsgIndex] = lastMsg;
          return { ...prev, messages };
        });
      });
    } catch (error) {
      console.error('Failed to send message:', error);
      // Remove optimistic messages on error
      setCurrentConversation((prev) => ({
        ...prev,
        messages: prev.messages.slice(0, -2),
      }));
      setIsLoading(false);
    }
  };

  return (
    <div className="app">
      <Sidebar
        conversations={conversations}
        currentConversationId={currentConversationId}
        onSelectConversation={handleSelectConversation}
        onNewConversation={handleNewConversation}
      />
      <ChatInterface
        conversation={currentConversation}
        onSendMessage={handleSendMessage}
        isLoading={isLoading}
      />
    </div>
  );
}

export default App;
