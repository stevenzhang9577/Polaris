// Dev-server-only fixture. The runner supplies synthetic HTTP responses and a Desktop bridge.
import React from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { PaperSummaryPanel } from '../src/features/wiki/PaperSummaryPanel';
import { ObsidianVaultSettings } from '../src/features/settings/ObsidianVaultSettings';
import { ToastHost } from '../src/components/ui/Toast';
import { probeLocalBackend } from '../src/lib/endpoint';
import { loadCapabilities } from '../src/lib/host';
import '../src/styles/global.css';

await probeLocalBackend();
await loadCapabilities();
const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={client}>
      <main style={{ padding: 24, maxWidth: 920, margin: 'auto' }}>
        {new URLSearchParams(location.search).get('view') === 'vault'
          ? <ObsidianVaultSettings />
          : <PaperSummaryPanel paperId="paper-test" libraryId="library-zotero" canManage />}
      </main>
      <ToastHost />
    </QueryClientProvider>
  </React.StrictMode>,
);
