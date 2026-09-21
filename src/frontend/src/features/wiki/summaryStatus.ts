import type { PaperSummaryRevision } from '../../lib/api';
import { tr } from '../../lib/i18n';

export function summaryUtcTime(value: string): string {
  return /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value.replace(' ', 'T')}Z`;
}

export function latestSummaryFailure(history: PaperSummaryRevision[]): PaperSummaryRevision | undefined {
  const ordered = [...history].sort((a, b) => Date.parse(summaryUtcTime(b.created_at)) - Date.parse(summaryUtcTime(a.created_at)));
  // A later successful attempt supersedes earlier errors, even if the user selected an older revision.
  const latest = ordered.find(row => ['failed', 'ready', 'stale'].includes(row.status));
  return latest?.status === 'failed' && latest.error_code !== 'SUMMARY_CANCELLED' ? latest : undefined;
}

export function summaryModelLabel(row: PaperSummaryRevision): string {
  if (row.requested_model) {
    if (row.model && row.model !== row.requested_model) return tr(`请求 ${row.requested_model} · 返回 ${row.model}`, `Requested ${row.requested_model} · returned ${row.model}`);
    if (row.model) return row.model;
    return tr(`请求 ${row.requested_model} · 尚无响应模型`, `Requested ${row.requested_model} · no response model yet`);
  }
  return row.model ?? tr('此历史记录未保存模型信息', 'This record has no saved model information');
}

export function summaryErrorLabel(code: string): string {
  const labels: Record<string, [string, string]> = {
    LLM_PROVIDER_PROTOCOL_MISMATCH: ['模型网关返回格式与配置协议不符；请检查供应商地址与协议', 'The gateway response does not match the configured protocol; check the provider URL and protocol'],
    LLM_EMPTY_RESPONSE: ['模型未返回正文；请检查网关响应或模型输出限制', 'The model returned no text; check the gateway response or output limit'],
    SUMMARY_CANCELLED: ['任务已取消', 'Task cancelled'],
    LLM_NOT_CONFIGURED: ['尚未配置可用的总结模型', 'No summary model is configured'],
    LLM_PROVIDER_UNAVAILABLE: ['无法连接模型服务，请检查供应商连接', 'Cannot reach the model provider'],
    LLM_PROVIDER_TIMEOUT: ['模型服务响应超时', 'The model provider timed out'],
    SUMMARY_GENERATION_FAILED: ['此历史尝试生成失败，未保存具体原因', 'This attempt failed without a saved detailed reason'],
  };
  return labels[code] ? tr(...labels[code]) : code;
}
