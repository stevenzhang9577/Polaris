// Dev-server-only fixture. The browser test supplies synthetic API responses.
import React from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { UsageDashboard } from '../src/features/settings/UsageDashboard';
import { LlmPricingSettings } from '../src/features/settings/LlmPricingSettings';
import { ProvidersSection, RoutesSection } from '../src/features/settings/SettingsPage';
import { ToastHost } from '../src/components/ui/Toast';
import '../src/styles/global.css';

const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const view = new URLSearchParams(location.search).get('view');
if (view === 'routing') Object.assign(window, { refetchRoutingForTest: () => client.refetchQueries({ queryKey: ['llm', 'routes'] }) });
createRoot(document.getElementById('root')!).render(
  <React.StrictMode><QueryClientProvider client={client}>
    <main style={{ padding: 32, maxWidth: 1440, margin: 'auto' }}>
      <p style={{ color: 'var(--muted)', marginBottom: 24 }}>Polaris · 用量统计预览（示例数据）</p>
      {view === 'routing'
        ? <RoutesSection />
        : view === 'providers'
          ? <ProvidersSection />
          : view === 'pricing'
            ? <LlmPricingSettings />
            : <UsageDashboard scope={view === 'platform' ? 'platform' : 'personal'} />}
    </main><ToastHost />
  </QueryClientProvider></React.StrictMode>,
);
