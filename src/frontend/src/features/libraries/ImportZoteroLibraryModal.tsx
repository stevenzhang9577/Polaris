import { useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { Modal } from '../../components/ui/Modal';
import { FormField } from '../../components/ui/FormField';
import { api } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { toast } from '../../components/ui/Toast';
import { libraryPath } from './hooks';
import { DisciplineSelect } from './DisciplineSelect';

export function ImportZoteroLibraryModal({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  const cache = useQueryClient();
  const [key, setKey] = useState('');
  const [name, setName] = useState('');
  const [statement, setStatement] = useState('');
  const [discipline, setDiscipline] = useState('');
  const [search, setSearch] = useState('');
  const [another, setAnother] = useState(false);
  const receipt = useRef({ payload: '', id: crypto.randomUUID() });
  const probe = useQuery({ queryKey: ['zotero-local-probe'], queryFn: api.probeZoteroLocal, retry: false, staleTime: 0 });
  const collections = useQuery({ queryKey: ['zotero-local-collections'], queryFn: api.listZoteroCollections, enabled: probe.data?.available === true, retry: false, staleTime: 0 });
  const bindings = useQuery({ queryKey: ['zotero-bindings'], queryFn: api.listZoteroBindings, retry: false, staleTime: 0 });
  const options = useMemo(() => (collections.data ?? []).map((c) => {
    const names = [c.name];
    const seen = new Set([c.key]);
    let parent = c.parent_key;
    while (parent && !seen.has(parent)) {
      seen.add(parent);
      const p = collections.data?.find((item) => item.key === parent);
      if (!p) break;
      names.unshift(p.name);
      parent = p.parent_key;
    }
    return { ...c, path: names.join(' / ') };
  }).sort((a, b) => a.path.localeCompare(b.path)), [collections.data]);
  const existing = bindings.data?.filter((b) => b.collection_key === key) ?? [];
  const mutation = useMutation({
    mutationFn: async () => {
      const payload = { collection_key: key, name: name.trim(), statement: statement.trim() || null, discipline: discipline || null };
      const fingerprint = JSON.stringify(payload);
      if (receipt.current.payload !== fingerprint) receipt.current = { payload: fingerprint, id: crypto.randomUUID() };
      return api.importZoteroLibrary({ ...payload, request_id: receipt.current.id });
    },
    onSuccess: (result) => {
      void cache.invalidateQueries({ queryKey: ['libraries'] });
      void cache.invalidateQueries({ queryKey: ['zotero-bindings'] });
      toast(result.dispatch_pending ? tr('文献库已创建，同步等待重试', 'Library created; sync will retry') : tr('文献库已创建，正在后台同步', 'Library created; syncing in the background'), 'ok');
      onClose();
      navigate(libraryPath(result.library_id, '?zotero=sync'));
    },
  });
  const close = () => { if (!mutation.isPending) onClose(); };
  return <Modal open onClose={close} title={tr('导入 Zotero 文献库', 'Import Zotero library')} width={620}
    sub={tr('选择一个分类，创建个人文献库并持续同步。', 'Choose a collection to create a personal library with ongoing sync.')}
    footer={<><button className="btn btn-ghost sm" onClick={close} disabled={mutation.isPending}>{tr('取消', 'Cancel')}</button>
      <button className="btn btn-primary sm" disabled={!key || !name.trim() || !probe.data?.available || bindings.isPending || bindings.isError || mutation.isPending || (existing.length > 0 && !another)} onClick={() => mutation.mutate()}>
        {mutation.isPending ? tr('创建并连接中…', 'Connecting…') : tr('创建并同步', 'Create and sync')}</button></>}>
    <div className="col gap16">
      <div role="status">{probe.isPending ? tr('正在检测 Zotero…', 'Detecting Zotero…') : probe.data?.available ? tr('已连接本机 Zotero', 'Local Zotero connected') : tr('请启动 Zotero，并在设置中启用本地 API。', 'Start Zotero and enable its local API in settings.')}
        <button className="btn btn-soft sm" style={{ marginLeft: 12 }} disabled={probe.isFetching} onClick={() => { void probe.refetch(); void collections.refetch(); void bindings.refetch(); }}>{tr('重新检测', 'Detect again')}</button>
      </div>
      {(probe.error || collections.error || bindings.error) && <p role="alert" style={{ color: 'var(--danger-tx)' }}>{String((probe.error || collections.error || bindings.error)?.message)}</p>}
      {probe.data?.available && <>
        <input className="input" aria-label={tr('搜索 Collection', 'Search collections')} placeholder={tr('搜索分类名称或父级路径', 'Search collection or parent path')} value={search} onChange={(e) => setSearch(e.target.value)} />
        <div role="radiogroup" aria-label="Zotero Collection" style={{ maxHeight: 220, overflowY: 'auto', border: '1px solid var(--border)', borderRadius: 10, padding: 10 }}>
          {collections.isPending && <p>{tr('正在读取分类…', 'Loading collections…')}</p>}
          {!collections.isPending && !options.length && <p>{tr('暂无分类，请先在 Zotero 中创建 Collection。', 'No collections. Create one in Zotero first.')}</p>}
          {options.filter((c) => c.path.toLowerCase().includes(search.toLowerCase())).map((c) => <label key={c.key} style={{ display: 'flex', gap: 10, padding: '8px 0', overflowWrap: 'anywhere' }}>
            <input type="radio" name="collection" checked={key === c.key} disabled={mutation.isPending} onChange={() => { setKey(c.key); setName(c.name); setAnother(false); }} />{c.path}
          </label>)}
        </div>
        {existing.length > 0 && <div className="card card-pad"><p>{tr('这个分类已连接文献库。', 'This collection is already connected.')}</p>
          {existing.map((b) => <button key={b.id} className="btn btn-soft sm" onClick={() => navigate(libraryPath(b.library_id, '?zotero=sync'))}>{tr('打开已有文献库', 'Open existing library')} · {b.collection_name}</button>)}
          <label style={{ display: 'block', marginTop: 12 }}><input type="checkbox" checked={another} onChange={(e) => setAnother(e.target.checked)} /> {tr('另建一个文献库', 'Create another library')}</label>
        </div>}
        {key && <>
          <FormField label={tr('文献库名称', 'Library name')}><input className="input" aria-label={tr('文献库名称', 'Library name')} maxLength={255} value={name} disabled={mutation.isPending} onChange={(e) => setName(e.target.value)} /></FormField>
          <FormField label={tr('方向描述（可选）', 'Statement (optional)')}><textarea className="textarea" maxLength={10000} rows={2} value={statement} disabled={mutation.isPending} onChange={(e) => setStatement(e.target.value)} /></FormField>
          <FormField label={tr('学科（可选）', 'Discipline (optional)')}><DisciplineSelect value={discipline} onChange={setDiscipline} disabled={mutation.isPending} /></FormField>
        </>}
        <p className="muted" style={{ fontSize: 12, lineHeight: 1.7 }}>{tr('包含全部子分类，同步论文信息并关联本机 PDF 路径；阅读时直接打开 Zotero 原文件，不复制 PDF。不会批量解析或生成总结，也不会修改 Zotero。之后每 15 分钟检查更新。', 'Includes all subcollections and links local PDF paths. Reading opens the original Zotero file without copying. No bulk parsing, summaries or changes to Zotero. Updates are checked every 15 minutes.')}</p>
      </>}
      {mutation.error && <p role="alert" style={{ color: 'var(--danger-tx)' }}>{mutation.error.message} · {tr('可重试，不会重复创建。', 'Retry will not create a duplicate.')}</p>}
    </div>
  </Modal>;
}
