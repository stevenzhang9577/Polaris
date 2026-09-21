import { describe, expect, it } from 'vitest';
import { hasUnsupportedOpenAiEndpointSuffix } from '../SettingsPage';

describe('provider Base URL validation', () => {
  const invalid = (transport: 'chat_completions' | 'responses', base_url: string) => (
    hasUnsupportedOpenAiEndpointSuffix({
      kind: 'openai_compat',
      transport,
      base_url,
    })
  );

  it('requires an API root for chat-completions providers', () => {
    expect(invalid('chat_completions', 'https://openrouter.ai/api/v1')).toBe(false);
    expect(invalid('chat_completions', 'https://openrouter.ai/api/v1/embeddings')).toBe(true);
    expect(invalid('chat_completions', 'https://openrouter.ai/api/v1/chat/completions')).toBe(true);
    expect(invalid('chat_completions', 'https://openrouter.ai/api/v1/responses')).toBe(true);
  });

  it('keeps the complete Responses URL supported by the backend', () => {
    expect(invalid('responses', 'https://relay.example/v1')).toBe(false);
    expect(invalid('responses', 'https://relay.example/v1/responses')).toBe(false);
    expect(invalid('responses', 'https://relay.example/v1/responses/')).toBe(false);
    expect(invalid('responses', 'https://relay.example/v1/RESPONSES')).toBe(true);
    expect(invalid('responses', 'https://relay.example/v1/embeddings')).toBe(true);
  });
});
