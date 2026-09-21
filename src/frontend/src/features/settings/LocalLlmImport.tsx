import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { Icon } from '../../components/ui/Icon';
import { Modal } from '../../components/ui/Modal';
import { Switch } from '../../components/ui/Switch';
import { toast } from '../../components/ui/Toast';
import {
  api,
  type LlmRoute,
  type LocalLlmConfigImportInput,
  type LocalLlmConfigPreview,
} from '../../lib/api';
import { localOrigin } from '../../lib/endpoint';
import {
  CAPABILITY_LLM_LOCAL_CONFIG_IMPORT,
  isCapabilityAvailable,
  loadCapabilities,
} from '../../lib/host';
import { tr } from '../../lib/i18n';

export type LocalImportTarget = 'provider' | 'default' | 'agent';

export function stagesForLocalImport(target: LocalImportTarget): string[] {
  if (target === 'default') return ['default'];
  if (target === 'agent') return ['agent'];
  return [];
}

export interface LocalImportRouteImpact {
  stage: string;
  action: 'create' | 'replace' | 'skip';
  currentModel: string | null;
}

export function localImportRouteImpact(
  stages: string[],
  routes: LlmRoute[],
  overwrite: boolean,
): LocalImportRouteImpact[] {
  const current = new Map(routes.map((route) => [route.stage, route]));
  return stages.map((stage) => {
    const route = current.get(stage);
    if (!route) return { stage, action: 'create', currentModel: null };
    return {
      stage,
      action: overwrite ? 'replace' : 'skip',
      currentModel: route.model,
    };
  });
}

function sourceLabel(config: LocalLlmConfigPreview): string {
  return config.source === 'codex' ? 'Codex' : 'Claude Code';
}

function credentialLabel(config: LocalLlmConfigPreview): string {
  switch (config.credential_status) {
    case 'available':
      return tr('凭据可导入', 'Credential available');
    case 'not_required':
      return tr('无需凭据', 'No credential required');
    case 'unsupported':
      return tr('登录态不可复用', 'Login session cannot be reused');
    default:
      return tr('缺少可复用凭据', 'Reusable credential missing');
  }
}

function actionLabel(action: LocalImportRouteImpact['action']): string {
  if (action === 'create') return tr('新增', 'Create');
  if (action === 'replace') return tr('替换', 'Replace');
  return tr('保留现有配置', 'Keep existing');
}

/**
 * Discovery 的 errors 即使由当前后端保证脱敏，也不原样渲染。这里只从固定前缀
 * 判断来源，原因统一用产品文案，避免未来底层异常意外把路径或 secret 带到界面。
 */
export function localConfigScanErrorLabel(error: string): string {
  if (error.startsWith('codex:')) {
    return tr('Codex 配置无法读取或格式无效。', 'The Codex configuration could not be read or is invalid.');
  }
  if (error.startsWith('claude_code:')) {
    return tr('Claude Code 配置无法读取或格式无效。', 'The Claude Code configuration could not be read or is invalid.');
  }
  return tr('一个本机模型配置无法安全检查。', 'One local model configuration could not be inspected safely.');
}

/** 后端 warning 只当作枚举码消费；冒号后的 env 名等动态内容不会进入 DOM。 */
export function localConfigWarningLabel(warning: string): string {
  const code = warning.split(':', 1)[0];
  switch (code) {
    case 'BASE_URL_MISSING':
      return tr('没有配置服务地址。', 'No service endpoint is configured.');
    case 'BASE_URL_INVALID':
      return tr('服务地址格式无效。', 'The service endpoint is invalid.');
    case 'CODEX_SIGN_IN_NOT_IMPORTABLE':
      return tr('Codex 订阅登录态不能作为 API 凭据导入。', 'A Codex subscription session cannot be imported as an API credential.');
    case 'CODEX_WIRE_API_UNSUPPORTED':
      return tr('该 Codex 请求协议暂不受支持。', 'This Codex transport is not supported.');
    case 'DEFAULT_MODEL_MISSING':
      return tr('没有可用于验证连接的默认模型。', 'No default model is available for connection validation.');
    case 'CREDENTIAL_ENV_MISSING':
    case 'ENV_REFERENCE_MISSING':
    case 'CREDENTIAL_MISSING':
      return tr('引用的 API 凭据在当前环境中不可用。', 'The referenced API credential is unavailable in this environment.');
    case 'AUTH_COMMAND_NOT_EXECUTED':
    case 'NESTED_AUTH_NOT_IMPORTED':
    case 'API_KEY_HELPER_NOT_EXECUTED':
      return tr('出于安全原因，不会执行外部取令牌命令或导入登录会话。', 'For safety, external token commands and sign-in sessions are not imported.');
    case 'ENV_HEADERS_NOT_IMPORTED':
    case 'STATIC_HEADERS_NOT_IMPORTED':
    case 'QUERY_PARAMS_NOT_IMPORTED':
      return tr('该连接依赖 Polaris 暂不支持的自定义请求参数。', 'This connection depends on custom request settings Polaris does not support.');
    case 'EFFORT_UNSUPPORTED':
      return tr('原配置的推理档位不受支持，将使用模型默认值。', 'The configured reasoning effort is unsupported; the model default will be used.');
    case 'MODEL_ALIAS_UNRESOLVED':
    case 'OPUSPLAN_DYNAMIC_ROUTING_NOT_IMPORTED':
      return tr('动态模型别名无法可靠解析，请先在源配置中指定具体模型。', 'A dynamic model alias could not be resolved reliably; choose a concrete model in the source configuration.');
    default:
      return tr('源配置包含当前无法安全导入的设置。', 'The source contains a setting that cannot be imported safely.');
  }
}

interface PendingImport {
  config: LocalLlmConfigPreview;
  target: LocalImportTarget;
}

/**
 * Desktop-only 配置发现入口。模型配置来自 Codex / Claude Code，真正的工具循环仍由
 * Polaris 执行；renderer 永远看不到密钥值。
 */
export function LocalLlmImport() {
  const queryClient = useQueryClient();
  const [available, setAvailable] = useState(
    () => localOrigin() !== null && isCapabilityAvailable(CAPABILITY_LLM_LOCAL_CONFIG_IMPORT),
  );
  const [targetByKey, setTargetByKey] = useState<Record<string, LocalImportTarget>>({});
  const [overwriteRoutes, setOverwriteRoutes] = useState(false);
  const [pending, setPending] = useState<PendingImport | null>(null);

  useEffect(() => {
    if (available || localOrigin() === null) return;
    let alive = true;
    void loadCapabilities()
      .then(() => {
        if (alive) {
          setAvailable(
            localOrigin() !== null
              && isCapabilityAvailable(CAPABILITY_LLM_LOCAL_CONFIG_IMPORT),
          );
        }
      })
      .catch(() => {
        // 旧宿主或桥故障：入口保持隐藏，绝不回退到远程后端扫描本机路径。
        if (alive) setAvailable(false);
      });
    return () => {
      alive = false;
    };
  }, [available]);

  const discovery = useQuery({
    queryKey: ['llm', 'local-configs'],
    queryFn: () => api.discoverLocalLlmConfigs(),
    enabled: available,
    retry: false,
  });
  const routes = useQuery({
    queryKey: ['llm', 'routes'],
    queryFn: () => api.getLlmRoutes(),
    enabled: available,
    retry: false,
  });
  const providers = useQuery({
    queryKey: ['llm', 'providers'],
    queryFn: () => api.listLlmProviders(),
    enabled: available,
    retry: false,
  });

  const selectedStages = pending ? stagesForLocalImport(pending.target) : [];
  const routeImpact = useMemo(
    () => localImportRouteImpact(selectedStages, routes.data ?? [], overwriteRoutes),
    [overwriteRoutes, routes.data, selectedStages],
  );

  const importMutation = useMutation({
    mutationFn: (input: LocalLlmConfigImportInput) => api.importLocalLlmConfig(input),
    onSuccess: (result) => {
      const routeNote = result.updated_stages.length > 0
        ? tr(`；已更新路由 ${result.updated_stages.join('、')}`, `; routed ${result.updated_stages.join(', ')}`)
        : '';
      const skipNote = result.skipped_stages.length > 0
        ? tr(`；保留已有路由 ${result.skipped_stages.join('、')}`, `; kept ${result.skipped_stages.join(', ')}`)
        : '';
      toast(
        `${result.created ? tr('模型连接已导入', 'Model connection imported') : tr('模型连接已刷新', 'Model connection refreshed')}${routeNote}${skipNote}`,
        'ok',
      );
      setPending(null);
      void queryClient.invalidateQueries({ queryKey: ['llm'] });
    },
    onError: (error) => {
      toast(
        `${tr('导入失败', 'Import failed')}：${error instanceof Error ? error.message : String(error)}`,
        'error',
      );
    },
  });

  if (!available) return null;

  const configs = discovery.data?.configs ?? [];
  const scanErrors = discovery.data?.errors ?? [];

  return (
    <div className="card card-pad" style={{ marginBottom: 20 }}>
      <div className="row" style={{ justifyContent: 'space-between', gap: 12, marginBottom: 8 }}>
        <div>
          <div className="section-h">
            <Icon name="cpu" size={15} style={{ color: 'var(--accent)' }} />
            {tr('复用本机模型配置', 'Reuse local model settings')}
          </div>
          <div style={{ marginTop: 5, fontSize: 11.5, lineHeight: 1.5, color: 'var(--text-3)' }}>
            {tr(
              '只读扫描 Codex 与 Claude Code 的模型连接。导入后由 Polaris 执行论文总结和工具循环，不会启动外部 CLI Agent。',
              'Read Codex and Claude Code model connections. Polaris runs the research and tool loop after import; external CLI agents are not started.',
            )}
          </div>
        </div>
        <button
          className="btn btn-soft sm"
          disabled={discovery.isFetching}
          onClick={() => void discovery.refetch()}
        >
          <Icon
            name="refresh"
            size={12}
            style={discovery.isFetching ? { animation: 'spin 1s linear infinite' } : undefined}
          />
          {discovery.isFetching ? tr('扫描中…', 'Scanning…') : tr('重新扫描', 'Scan again')}
        </button>
      </div>

      <div
        style={{
          borderRadius: 8,
          padding: '9px 11px',
          marginBottom: 12,
          background: 'var(--surface-2)',
          color: 'var(--text-3)',
          fontSize: 11,
          lineHeight: 1.45,
        }}
      >
        {tr(
          '安全边界：不会读取 Codex auth.json、Claude 登录会话或执行取令牌命令；密钥不会返回到界面。官方订阅登录态不等同于 API 凭据。',
          'Security boundary: Polaris never reads Codex auth.json, Claude login sessions, or runs token commands. Secrets never reach this page. Subscription login is not an API credential.',
        )}
      </div>

      {!discovery.isLoading && !discovery.isError && scanErrors.length > 0 && (
        <div
          role="status"
          style={{
            borderRadius: 8,
            padding: '9px 11px',
            marginBottom: 12,
            background: 'var(--warn-bg)',
            color: 'var(--warn-tx)',
            fontSize: 11,
            lineHeight: 1.5,
          }}
        >
          <div style={{ fontWeight: 620, marginBottom: 3 }}>
            {tr('部分本机配置未能扫描', 'Some local settings could not be scanned')}
          </div>
          {[...new Set(scanErrors.map(localConfigScanErrorLabel))].map((label) => (
            <div key={label}>• {label}</div>
          ))}
          <div style={{ marginTop: 4 }}>
            {tr('已识别的其他配置仍可单独导入；现有 Polaris 配置不会被修改。', 'Other discovered settings can still be imported individually; existing Polaris settings were not changed.')}
          </div>
        </div>
      )}

      {discovery.isLoading ? (
        <div className="empty" style={{ padding: 22 }}>{tr('正在检查本机配置…', 'Checking local settings…')}</div>
      ) : discovery.isError ? (
        <div className="empty" style={{ padding: 22 }}>
          {tr('无法读取本机配置。可以重新扫描；现有模型配置不会受影响。', 'Could not inspect local settings. Existing model settings were not changed.')}
        </div>
      ) : configs.length === 0 ? (
        <div className="empty" style={{ padding: 22 }}>
          {tr('没有发现可识别的 Codex 或 Claude Code 自定义模型配置。', 'No supported Codex or Claude Code custom model settings were found.')}
        </div>
      ) : (
        <div style={{ display: 'grid', gap: 10 }}>
          {configs.map((config) => {
            const key = `${config.source}:${config.source_key}`;
            const target = targetByKey[key] ?? 'provider';
            const imported = providers.data?.find((provider) => provider.id === config.existing_provider_id);
            const sourceChanged = imported != null
              && imported.import_fingerprint !== config.fingerprint;
            return (
              <div
                key={key}
                style={{ border: '0.5px solid var(--border)', borderRadius: 9, padding: 12 }}
              >
                <div className="row local-llm-import-row" style={{ justifyContent: 'space-between', gap: 12, alignItems: 'flex-start' }}>
                  <div style={{ minWidth: 0 }}>
                    <div className="row gap6" style={{ flexWrap: 'wrap' }}>
                      <strong style={{ fontSize: 12.5 }}>{sourceLabel(config)}</strong>
                      <span className="tag mono" style={{ fontSize: 10.5 }}>{config.transport}</span>
                      {config.effort && (
                        <span className="tag mono" style={{ fontSize: 10.5 }}>
                          effort={config.effort}
                        </span>
                      )}
                      <span
                        className="pill sm"
                        style={config.importable
                          ? { background: 'var(--ok-bg)', color: 'var(--ok-tx)' }
                          : { background: 'var(--warn-bg)', color: 'var(--warn-tx)' }}
                      >
                        {credentialLabel(config)}
                      </span>
                      {config.existing_provider_id && (
                        <span
                          className="pill sm"
                          style={sourceChanged
                            ? { background: 'var(--warn-bg)', color: 'var(--warn-tx)' }
                            : { background: 'var(--accent-soft)', color: 'var(--accent-text)' }}
                        >
                          {sourceChanged
                            ? tr('源配置有更新', 'Source changed')
                            : tr('已同步', 'In sync')}
                        </span>
                      )}
                    </div>
                    <div style={{ marginTop: 5, fontSize: 12 }}>{config.display_name}</div>
                    <div className="mono" style={{ marginTop: 3, fontSize: 10.5, color: 'var(--text-3)' }}>
                      {config.endpoint_origin ?? tr('未配置 Endpoint', 'Endpoint not configured')}
                    </div>
                    <div className="row gap6" style={{ marginTop: 7, flexWrap: 'wrap' }}>
                      {config.models.map((model) => (
                        <span className="tag mono" style={{ fontSize: 10.5 }} key={model}>
                          {model}{model === config.default_model ? ` · ${tr('默认', 'default')}` : ''}
                        </span>
                      ))}
                    </div>
                  </div>
                  <div className="local-llm-import-actions" style={{ width: 184, flexShrink: 0 }}>
                    <select
                      className="input"
                      aria-label={tr('导入后的路由用途', 'Route after import')}
                      value={target}
                      disabled={!config.importable}
                      onChange={(event) => setTargetByKey((current) => ({
                        ...current,
                        [key]: event.target.value as LocalImportTarget,
                      }))}
                      style={{ width: '100%', height: 32, fontSize: 11.5 }}
                    >
                      <option value="provider">{tr('只导入连接', 'Connection only')}</option>
                      <option value="default">{tr('设为默认模型', 'Set as default')}</option>
                      <option value="agent">{tr('仅用于 Agent', 'Agent only')}</option>
                    </select>
                    <button
                      className="btn btn-primary sm"
                      style={{ width: '100%', marginTop: 7, justifyContent: 'center' }}
                      disabled={!config.importable || importMutation.isPending}
                      onClick={() => {
                        setOverwriteRoutes(target !== 'provider');
                        setPending({ config, target });
                      }}
                    >
                      <Icon name={config.existing_provider_id ? 'refresh' : 'download'} size={12} />
                      {config.existing_provider_id ? tr('检查并刷新', 'Review & refresh') : tr('检查并导入', 'Review & import')}
                    </button>
                  </div>
                </div>
                {config.warnings.length > 0 && (
                  <div style={{ marginTop: 9, fontSize: 11, lineHeight: 1.45, color: 'var(--warn-tx)' }}>
                    {config.warnings.map((warning, index) => (
                      <div key={`${warning.split(':', 1)[0]}-${index}`}>
                        • {localConfigWarningLabel(warning)}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      <Modal
        open={pending !== null}
        onClose={() => { if (!importMutation.isPending) setPending(null); }}
        title={tr('确认导入本机模型配置', 'Confirm local model import')}
        sub={pending
          ? `${sourceLabel(pending.config)} · ${pending.config.default_model ?? pending.config.display_name}`
          : undefined}
        footer={(
          <>
            <button className="btn btn-ghost" disabled={importMutation.isPending} onClick={() => setPending(null)}>
              {tr('取消', 'Cancel')}
            </button>
            <button
              className="btn btn-primary"
              disabled={!pending || importMutation.isPending}
              onClick={() => {
                if (!pending) return;
                importMutation.mutate({
                  source: pending.config.source,
                  source_key: pending.config.source_key,
                  stages: stagesForLocalImport(pending.target),
                  overwrite_routes: overwriteRoutes,
                });
              }}
            >
              {importMutation.isPending ? tr('验证并导入中…', 'Validating & importing…') : tr('验证连接并导入', 'Validate & import')}
            </button>
          </>
        )}
      >
        {pending && (
          <div style={{ display: 'grid', gap: 13, fontSize: 12 }}>
            <div>
              <div style={{ color: 'var(--text-3)', marginBottom: 4 }}>{tr('将创建或刷新', 'Will create or refresh')}</div>
              <div><strong>{pending.config.display_name}</strong></div>
              <div className="mono" style={{ marginTop: 3, color: 'var(--text-3)', fontSize: 10.5 }}>
                {pending.config.transport} · {pending.config.auth_scheme} · {pending.config.endpoint_origin ?? '—'}
              </div>
            </div>

            {routeImpact.length === 0 ? (
              <div style={{ color: 'var(--text-3)' }}>
                {tr('现有任务继续使用原连接。若源连接已变化且正在被使用，会另建连接；你可以随后在路由表中选择。', 'Existing tasks keep their connection. If an in-use source connection changed, a new connection is created for you to assign later.')}
              </div>
            ) : (
              <div>
                <div style={{ color: 'var(--text-3)', marginBottom: 6 }}>{tr('只迁移下列选择替换的路由；其余任务保留原连接。', 'Only routes selected for replacement move; other tasks keep their connection.')}</div>
                {routeImpact.map((impact) => (
                  <div key={impact.stage} className="row" style={{ justifyContent: 'space-between', padding: '5px 0', borderTop: '0.5px solid var(--border)' }}>
                    <span className="mono">{impact.stage}</span>
                    <span style={{ color: impact.action === 'replace' ? 'var(--warn-tx)' : 'var(--text-3)' }}>
                      {actionLabel(impact.action)}
                      {impact.currentModel ? ` · ${impact.currentModel}` : ''}
                    </span>
                  </div>
                ))}
              </div>
            )}

            {routeImpact.some((impact) => impact.currentModel !== null) && (
              <div className="row" style={{ justifyContent: 'space-between', gap: 12 }}>
                <div>
                  <div style={{ fontWeight: 620 }}>{tr('覆盖已有路由', 'Replace existing routes')}</div>
                  <div style={{ color: 'var(--text-3)', fontSize: 10.8, marginTop: 2 }}>
                    {tr('默认关闭；关闭时保留当前模型。', 'Off by default; current models are kept when off.')}
                  </div>
                </div>
                <Switch
                  checked={overwriteRoutes}
                  onChange={setOverwriteRoutes}
                  aria-label={tr('覆盖已有路由', 'Replace existing routes')}
                />
              </div>
            )}

            <div style={{ borderRadius: 7, padding: 9, background: 'var(--surface-2)', color: 'var(--text-3)', fontSize: 10.8, lineHeight: 1.45 }}>
              {tr(
                '导入时本地后端会重新读取配置并发起最小连接测试。测试失败不会启用新连接，也不会改变现有路由。',
                'The local backend re-reads the source and runs a minimal connection test. A failed test does not enable the connection or change existing routes.',
              )}
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}
