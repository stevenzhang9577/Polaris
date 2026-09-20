// Dev-server-only fixture. The runner supplies synthetic HTTP responses and a Desktop bridge.
import React from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { PaperSummaryPanel } from '../src/features/wiki/PaperSummaryPanel';
import { ObsidianVaultSettings } from '../src/features/settings/ObsidianVaultSettings';
import { ToastHost } from '../src/components/ui/Toast';
import { probeLocalBackend } from '../src/lib/endpoint';
import { loadCapabilities } from '../src/lib/host';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { LibrariesPage } from '../src/features/libraries/LibrariesPage';
import { PythonEnvironmentSettings } from '../src/features/settings/PythonEnvironmentSettings';
import { SettingsPage } from '../src/features/settings/SettingsPage';
import { PdfReader } from '../src/features/reading/PdfReader';
import { PapersTab } from '../src/features/wiki/PapersTab';
import type { PaperDetail } from '../src/lib/api';
import '../src/styles/global.css';

await probeLocalBackend();
await loadCapabilities();
const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={client}>
      <main style={{ padding: 24, maxWidth: 920, margin: 'auto' }}>
        {new URLSearchParams(location.search).get('view') === 'reader'
          ? <div style={{ height: 700, display: 'flex', flexDirection: 'column' }}><PdfReader
              paper={{ id: 'paper-test', title: 'Original PDF', pdf_available: false,
                zotero_source: true, zotero_pdf_status: 'linked', zotero_library_id: 'library-zotero',
                can_materialize_zotero: true } as PaperDetail}
              libraryId="library-zotero" highlights={[]} activeHighlightId={null} creating={false}
              onCreateHighlight={() => undefined} onHighlightClick={() => undefined} jumpTarget={null}
            /></div>
          : new URLSearchParams(location.search).get('view') === 'summary-batches'
          ? <MemoryRouter><div className="card split-card" style={{ height: 780 }}><PapersTab libraryId="library-summary" canManage selectedId={null} onSelect={() => undefined} onOpenConcept={() => undefined} onWikiLink={() => undefined} /></div></MemoryRouter>
          : new URLSearchParams(location.search).get('view') === 'summary-settings'
          ? <MemoryRouter initialEntries={['/settings?tab=summaries']}><SettingsPage /></MemoryRouter>
          : new URLSearchParams(location.search).get('view') === 'settings'
          ? <MemoryRouter initialEntries={['/settings?tab=python']}><SettingsPage /></MemoryRouter>
          : new URLSearchParams(location.search).get('view') === 'libraries'
          ? <MemoryRouter><Routes><Route path="/" element={<LibrariesPage />} /><Route path="/libraries/:id" element={<div>Imported library destination</div>} /></Routes></MemoryRouter>
          : new URLSearchParams(location.search).get('view') === 'python'
          ? <PythonEnvironmentSettings />
          : new URLSearchParams(location.search).get('view') === 'vault'
          ? <ObsidianVaultSettings />
          : <PaperSummaryPanel paperId="paper-test" libraryId="library-zotero" canManage />}
      </main>
      <ToastHost />
    </QueryClientProvider>
  </React.StrictMode>,
);
