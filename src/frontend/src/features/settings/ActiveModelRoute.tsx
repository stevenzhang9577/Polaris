import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../../lib/api';
import { tr } from '../../lib/i18n';
import { stageLabel } from '../../lib/stageLabels';
import { toast } from '../../components/ui/Toast';

export function ActiveModelRoute() {
  const client = useQueryClient();
  const routes = useQuery({ queryKey: ['llm', 'routes'], queryFn: () => api.getLlmRoutes() });
  const providers = useQuery({ queryKey: ['llm', 'providers'], queryFn: () => api.listLlmProviders() });
  const [selection, setSelection] = useState('');
  const current = routes.data?.find(route => route.stage === 'default');
  const provider = providers.data?.find(item => item.id === current?.provider_id);
  const save = useMutation({
    mutationFn: () => {
      const [provider_id, model] = JSON.parse(selection) as [string, string];
      return api.putLlmRoutes([
        ...(routes.data ?? []).filter(route => route.stage !== 'default'),
        { stage: 'default', provider_id, model },
      ]);
    },
    onSuccess: () => {
      setSelection('');
      void client.invalidateQueries({ queryKey: ['llm', 'routes'] });
      toast(tr('默认路由已切换，后续调用生效；已发出的请求保持原模型', 'Default route saved for future requests; in-flight requests keep their model'), 'ok');
    },
    onError: (error) => toast(error instanceof Error ? error.message : String(error), 'error'),
  });
  return <div className="card card-pad usage-section">
    <div className="section-h">{tr('当前默认调用模型', 'Current default model')}</div>
    <p>{provider?.name ?? '—'} · <strong className="mono">{current?.model ?? tr('未配置', 'Not configured')}</strong></p>
    <p className="usage-note">{tr('供应商里的模型列表只代表可选模型；切换实际调用需要保存路由。下方用量是历史记录，不会随切换改名。', 'Provider model lists contain available models. Save routing to change actual requests. Historical usage keeps its original model.')}</p>
    <div className="row gap8 wrap">
      <select className="input" style={{ maxWidth: 400 }} aria-label={tr('切换默认模型', 'Switch default model')} value={selection} onChange={event => setSelection(event.target.value)}>
        <option value="">{tr('选择供应商与模型', 'Choose provider and model')}</option>
        {providers.data?.filter(item => item.enabled).flatMap(item => (item.models ?? []).map(model => <option key={`${item.id}:${model}`} value={JSON.stringify([item.id, model])}>{item.name} · {model}</option>))}
      </select>
      <button className="btn btn-primary sm" disabled={!selection || save.isPending || !routes.isSuccess || routes.isFetching} onClick={() => save.mutate()}>{tr('设为默认调用模型', 'Set default model')}</button>
    </div>
    {routes.data?.filter(route => route.stage !== 'default').map(route => <p className="usage-subtext" key={route.stage}>{tr(stageLabel(route.stage).zh, stageLabel(route.stage).en)}: {route.model} · {tr('单独配置，不跟随默认', 'Explicit override')}</p>)}
  </div>;
}
