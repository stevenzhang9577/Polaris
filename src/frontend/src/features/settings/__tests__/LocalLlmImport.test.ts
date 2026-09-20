import { describe, expect, it } from 'vitest';

import type { LlmRoute } from '../../../lib/api';
import {
  localConfigScanErrorLabel,
  localConfigWarningLabel,
  localImportRouteImpact,
  stagesForLocalImport,
} from '../LocalLlmImport';

describe('local LLM import route preview', () => {
  it('keeps provider-only imports away from the route table', () => {
    expect(stagesForLocalImport('provider')).toEqual([]);
    expect(stagesForLocalImport('default')).toEqual(['default']);
    expect(stagesForLocalImport('agent')).toEqual(['agent']);
  });

  it('shows create, skip, and explicit replacement without touching other stages', () => {
    const routes: LlmRoute[] = [
      {
        stage: 'default',
        provider_id: 'provider-old',
        model: 'old-model',
      },
      {
        stage: 'writing',
        provider_id: 'provider-writing',
        model: 'writing-model',
      },
    ];

    expect(localImportRouteImpact(['agent'], routes, false)).toEqual([
      { stage: 'agent', action: 'create', currentModel: null },
    ]);
    expect(localImportRouteImpact(['default'], routes, false)).toEqual([
      { stage: 'default', action: 'skip', currentModel: 'old-model' },
    ]);
    expect(localImportRouteImpact(['default'], routes, true)).toEqual([
      { stage: 'default', action: 'replace', currentModel: 'old-model' },
    ]);
  });
});

describe('local LLM import diagnostics', () => {
  it('never reflects scan error details that could contain a path or secret', () => {
    const raw = 'codex:CONFIG_INVALID:/Users/alice/.codex/config.toml?token=secret';
    const label = localConfigScanErrorLabel(raw);
    expect(label).toContain('Codex');
    expect(label).not.toContain('/Users/alice');
    expect(label).not.toContain('secret');

    const unknown = localConfigScanErrorLabel('unknown:/private/path?api_key=secret');
    expect(unknown).not.toContain('/private/path');
    expect(unknown).not.toContain('secret');
  });

  it('maps dynamic warning payloads to fixed copy', () => {
    const label = localConfigWarningLabel('ENV_REFERENCE_MISSING:VERY_SECRET_ENV_NAME');
    expect(label).not.toContain('VERY_SECRET_ENV_NAME');
    expect(label).toContain('凭据');

    const fallback = localConfigWarningLabel('FUTURE_WARNING:/private/path');
    expect(fallback).not.toContain('/private/path');
  });
});
