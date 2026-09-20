import { useEffect, useRef, useState } from 'react';
import { Icon } from '../../components/ui/Icon';
import { LangToggle } from '../../components/ui/LangToggle';
import { PolarisMark, PolarisWordmark } from '../../components/ui/PolarisLogo';
import {
  engineBootstrapStatus,
  kernelLocalBackend,
  type EngineBootstrapStatus,
} from '../../lib/host';
import { tr, useLang } from '../../lib/i18n';
import {
  BOOTSTRAP_STEPS,
  bootstrapFailureKind,
  bootstrapGate,
  bootstrapStepIndex,
} from './engineBootstrap';

/**
 * 桌面端首启等待页（#721）：窗口先起、内核在后台准备本机引擎，这里轮询
 * kernel.engineBootstrapStatus 给用户看得见的进度，而不是几分钟的白屏。
 *
 * 就绪后的放行分两条路：
 * - 探测到本地引擎地址 → 整页 reload。当前文档是引擎起来**之前**加载的，
 *   CSP 里没放行 127.0.0.1，不重载的话对本地引擎的一切请求都会被拦；
 *   重载后 main.tsx 的启动探测直接命中就绪状态，不会再回到这页。
 * - 没有本地引擎（idle / failed 后用户选择远程）→ onProceed() 进应用，
 *   App 按既有逻辑落到服务器配置页或远端流程。
 */
export function EngineBootstrapPage({
  initialStatus,
  onProceed,
}: {
  initialStatus: EngineBootstrapStatus;
  onProceed: () => void;
}) {
  useLang();
  const [status, setStatus] = useState(initialStatus);
  const [leaving, setLeaving] = useState(false);
  const proceededRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    const id = window.setInterval(() => {
      void engineBootstrapStatus().then((next) => {
        if (!cancelled && next) setStatus(next);
      });
    }, 1000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  const gate = bootstrapGate(status);
  const failureKind = bootstrapFailureKind(status);

  useEffect(() => {
    if (gate !== 'proceed' || proceededRef.current) return;
    proceededRef.current = true;
    setLeaving(true);
    void kernelLocalBackend().then((info) => {
      if (info?.baseUrl) {
        window.location.reload();
      } else {
        onProceed();
      }
    });
  }, [gate, onProceed]);

  const active = bootstrapStepIndex(status.phase);

  return (
    <div className="auth-page">
      <div className="auth-brand">
        <PolarisMark size={56} />
        <PolarisWordmark height={32} />
      </div>

      <div className="auth-lang">
        <LangToggle />
      </div>

      <div className="auth-card fadeup">
        {gate === 'failed' ? (
          <>
            {failureKind === 'legacy-encryption-migration' ? (
              <>
                <div className="auth-card-title">
                  {tr('已保护旧版加密凭据', 'Legacy encrypted credentials were protected')}
                </div>
                <div className="auth-card-sub" style={{ marginBottom: 10 }}>
                  {tr(
                    'Polaris 检测到由旧本机密钥加密的凭据，因此在创建新密钥前停止了升级。数据库没有被修改，凭据也没有被覆盖。',
                    'Polaris found credentials encrypted with the previous local key, so it stopped before creating a replacement key. The database and credentials were not changed.',
                  )}
                </div>
                <ol style={{ margin: '0 0 14px', paddingLeft: 20, color: 'var(--text-2)', fontSize: 12, lineHeight: 1.65 }}>
                  <li>{tr('退出 Polaris。', 'Quit Polaris.')}</li>
                  <li>
                    {tr(
                      '找回此前使用的 POLARIS_ENCRYPTION_KEY，并用完全相同的值重新启动 Polaris。它是 Fernet 主密钥，不是 Codex、Claude 或模型 API Key。',
                      'Restore the exact POLARIS_ENCRYPTION_KEY used previously, then launch Polaris with it. This is the Fernet master key, not a Codex, Claude, or model API key.',
                    )}
                  </li>
                  <li>
                    {tr(
                      '在完成凭据重加密迁移前，请备份并保留原密钥和本机数据；不要清空数据库、删除 secrets 目录或重装来绕过此提示。',
                      'Keep a backup of the previous key and local data until credential re-encryption is completed. Do not clear the database, delete the secrets directory, or reinstall to bypass this safeguard.',
                    )}
                  </li>
                </ol>
              </>
            ) : (
              <>
                <div className="auth-card-title">{tr('本机环境没有准备好', 'Local setup did not finish')}</div>
                <div className="auth-card-sub">
                  {tr(
                    '准备本机运行环境时出了问题。你可以改用远程服务器继续使用；重启应用会再试一次本机环境。',
                    'Something went wrong while preparing the local environment. You can continue with a remote server; restarting the app will retry the local setup.',
                  )}
                </div>
              </>
            )}
            <button
              type="button"
              className="btn btn-primary"
              style={{ width: '100%', justifyContent: 'center', height: 38 }}
              onClick={() => {
                if (!proceededRef.current) {
                  proceededRef.current = true;
                  onProceed();
                }
              }}
            >
              <Icon name="arrow" size={14} />
              {failureKind === 'legacy-encryption-migration'
                ? tr('暂用远程服务器（保留本机数据）', 'Use a remote server for now (keep local data)')
                : tr('使用远程服务器', 'Use a remote server')}
            </button>
          </>
        ) : (
          <>
            <div className="auth-card-title">
              {leaving ? tr('马上就好…', 'Almost there…') : tr('正在准备本地环境', 'Preparing the local environment')}
            </div>
            <div className="auth-card-sub">
              {tr('首次启动需要几分钟，之后会快很多。', 'The first launch takes a few minutes; later launches are much faster.')}
            </div>
            <div style={{ display: 'grid', gap: 10, marginTop: 4 }}>
              {BOOTSTRAP_STEPS.map((step, i) => {
                const stepDone = active > i || leaving;
                const stepActive = active === i && !leaving;
                return (
                  <div
                    key={step.en}
                    className="row"
                    style={{
                      gap: 8,
                      fontSize: 13,
                      color: stepActive ? 'var(--text-1)' : stepDone ? 'var(--ok-tx)' : 'var(--text-4)',
                      fontWeight: stepActive ? 600 : 400,
                    }}
                  >
                    {stepActive ? (
                      <Icon name="refresh" size={13} style={{ animation: 'spin 1s linear infinite' }} />
                    ) : (
                      <Icon name={stepDone ? 'check' : 'dot'} size={13} />
                    )}
                    {tr(step.zh, step.en)}
                  </div>
                );
              })}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
