/* ============================================================
   Desktop 内嵌后端的 Fernet 主密钥。

   后端会用 POLARIS_ENCRYPTION_KEY 加密 LLM/SSH 等凭据；若桌面壳不显式
   注入，开发默认值最终会从公开的 dev secret 派生，等于所有安装共用同一把
   已知钥匙。本模块为每个 userData 生成独立随机 key，并只在拉起 legacy-engine
   的极短窗口放进子进程环境。

   这与 renderer 登录 token 是两回事。后者仍留在浏览器 storage；仓库已验证
   当前 macOS ad-hoc 签名下 safeStorage 会因签名每次变化反复弹钥匙串授权框。
   因此当前 macOS 发行对这把本机后端主密钥使用专用的 owner-only 文件；它不在
   config.json，目录/文件权限分别固定为 0700/0600。Windows 以及有真实密码
   管理器的 Linux 优先用 Electron safeStorage，文件里只落密文。等 macOS 改为
   稳定 Developer ID 签名后，可把 allowSafeStorage 翻为 true 并迁移旧格式。

   owner-only 是有意收窄的回退：防止其他系统用户和普通配置/日志泄漏，但不声称
   能抵御已控制当前 OS 账号的攻击者。任何损坏、解密失败或权限收紧失败都 fail
   closed；绝不静默轮换，否则数据库里既有密文会永久不可恢复。
   ============================================================ */

import { randomBytes } from 'node:crypto';
import {
  chmodSync,
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';

const SECURE_HEADER = Buffer.from('POLARIS_ENGINE_KEY_V1_SECURE\n', 'utf8');
const OWNER_ONLY_HEADER = Buffer.from('POLARIS_ENGINE_KEY_V1_OWNER_ONLY\n', 'utf8');
const MAX_SECRET_FILE_BYTES = 16 * 1024;

/**
 * Fernet 密文所在的结构化列。保留旧 ssh_credentials 是为了能审计尚未跑到
 * connection_credentials 重命名迁移的 Desktop 库。
 */
const ENCRYPTED_COLUMNS: ReadonlyArray<readonly [string, readonly string[]]> = [
  ['llm_providers', ['api_key_encrypted']],
  ['connection_credentials', [
    'private_key_encrypted',
    'passphrase_encrypted',
    'payload_encrypted',
  ]],
  ['ssh_credentials', ['private_key_encrypted', 'passphrase_encrypted']],
  ['chat_bot_configs', ['robot_id_encrypted', 'secret_encrypted']],
  ['mcp_servers', ['env_encrypted']],
];

export const LEGACY_ENGINE_SECRETS_ERROR = 'DESKTOP_ENCRYPTION_MIGRATION_REQUIRED';

export interface SafeStorageLike {
  isEncryptionAvailable(): boolean;
  encryptString(plainText: string): Buffer;
  decryptString(encrypted: Buffer): string;
  getSelectedStorageBackend?(): string;
}

export interface EngineSecretOptions {
  dataDir: string;
  platform: NodeJS.Platform;
  safeStorage: SafeStorageLike;
  /**
   * 当前 macOS ad-hoc 包必须传 false；稳定 Developer ID 签名后才可翻 true。
   * 其他平台通常传 true，仍会检查真实 safeStorage backend 是否可用。
   */
  allowSafeStorage: boolean;
}

function secretPath(dataDir: string): string {
  return join(dataDir, 'secrets', 'engine-fernet-key');
}

function isFernetKey(value: string): boolean {
  if (!/^[A-Za-z0-9_-]{43}=$/.test(value)) return false;
  try {
    return Buffer.from(value.replace(/-/g, '+').replace(/_/g, '/'), 'base64').length === 32;
  } catch {
    return false;
  }
}

function generateFernetKey(): string {
  // Node 的 base64 保留末尾 '='，替换两个字符后正好是 Fernet 的 urlsafe base64。
  return randomBytes(32).toString('base64').replace(/\+/g, '-').replace(/\//g, '_');
}

function secureStorageAvailable(options: EngineSecretOptions): boolean {
  if (!options.allowSafeStorage || !options.safeStorage.isEncryptionAvailable()) return false;
  if (options.platform !== 'linux') return true;
  // Electron 在 Linux 无 password manager 时的 basic_text 只是可逆混淆，不能
  // 当作安全存储；unknown 表示调用早于 ready，同样不使用。
  const backend = options.safeStorage.getSelectedStorageBackend?.() ?? 'unknown';
  return backend !== 'basic_text' && backend !== 'unknown';
}

function ensurePrivateDirectory(path: string): void {
  mkdirSync(path, { recursive: true, mode: 0o700 });
  const stat = lstatSync(path);
  if (!stat.isDirectory() || stat.isSymbolicLink()) {
    throw new Error('desktop engine secret directory is not a private directory');
  }
  // Windows 会忽略 POSIX mode；Unix 上失败则抛出，避免误以为权限已经收紧。
  chmodSync(path, 0o700);
}

function assertRegularSecretFile(path: string): void {
  const stat = lstatSync(path);
  if (!stat.isFile() || stat.isSymbolicLink()) {
    throw new Error('desktop engine secret is not a regular file');
  }
  if (stat.size <= 0 || stat.size > MAX_SECRET_FILE_BYTES) {
    throw new Error('desktop engine secret has an invalid size');
  }
  chmodSync(path, 0o600);
}

function atomicWriteSecret(path: string, payload: Buffer): void {
  const tmp = `${path}.tmp-${randomBytes(8).toString('hex')}`;
  try {
    writeFileSync(tmp, payload, { flag: 'wx', mode: 0o600 });
    chmodSync(tmp, 0o600);
    renameSync(tmp, path);
    chmodSync(path, 0o600);
  } catch (error) {
    try {
      if (existsSync(tmp)) unlinkSync(tmp);
    } catch {
      /* 只清理本函数刚创建的随机临时文件；原异常更重要。 */
    }
    throw error;
  }
}

function quoteIdentifier(identifier: string): string {
  // 调用方只有上面的源码常量；仍统一 quote，避免列名未来撞 SQLite 关键字。
  return `"${identifier.replace(/"/g, '""')}"`;
}

function tableColumns(db: DatabaseSync, table: string): Set<string> {
  const rows = db.prepare(`PRAGMA table_info(${quoteIdentifier(table)})`).all() as Array<{
    name?: unknown;
  }>;
  return new Set(rows.map((row) => row.name).filter((name): name is string => typeof name === 'string'));
}

/**
 * 只回答“有没有旧 Fernet 载荷”，不把任何密文取回 JS、更不会写日志。
 * 无法证明安全（损坏库、异常 schema、JSON 不合法）时抛错，由上层 fail closed。
 */
export function databaseHasLegacyEncryptedSecrets(databasePath: string): boolean {
  if (!existsSync(databasePath)) return false;
  const stat = lstatSync(databasePath);
  if (!stat.isFile() || stat.isSymbolicLink()) {
    throw new Error('desktop database is not a regular file');
  }
  if (stat.size === 0) return false;

  let db: DatabaseSync | null = null;
  try {
    db = new DatabaseSync(databasePath, {
      readOnly: true,
      enableForeignKeyConstraints: false,
      allowExtension: false,
    });
    const tables = new Set(
      (db.prepare("SELECT name FROM sqlite_master WHERE type = 'table'").all() as Array<{
        name?: unknown;
      }>)
        .map((row) => row.name)
        .filter((name): name is string => typeof name === 'string'),
    );

    for (const [table, candidates] of ENCRYPTED_COLUMNS) {
      if (!tables.has(table)) continue;
      const columns = tableColumns(db, table);
      for (const column of candidates) {
        if (!columns.has(column)) continue;
        const found = db.prepare(
          `SELECT 1 AS found FROM ${quoteIdentifier(table)} `
          + `WHERE ${quoteIdentifier(column)} IS NOT NULL `
          + `AND trim(CAST(${quoteIdentifier(column)} AS TEXT)) <> '' LIMIT 1`,
        ).get();
        if (found) return true;
      }
    }

    // 两份部署设置把 Fernet token 放在 JSON 文档的 credential pool 里，而不是
    // 独立 *_encrypted 列。json_tree 仍只返回常量 1，不把 secret 拉到 JS。
    if (tables.has('system_settings')) {
      const columns = tableColumns(db, 'system_settings');
      if (columns.has('key') && columns.has('value')) {
        const found = db.prepare(
          "SELECT 1 AS found FROM system_settings AS setting, json_tree(setting.value) AS node "
          + "WHERE setting.key IN ('literature_search', 'document_processing') "
          + "AND node.key = 'secret' AND node.type = 'text' "
          + "AND trim(CAST(node.value AS TEXT)) <> '' LIMIT 1",
        ).get();
        if (found) return true;
      }
    }
    return false;
  } catch (error) {
    const detail = error instanceof Error ? error.message : 'unknown sqlite error';
    throw new Error(`desktop database encryption audit failed: ${detail}`);
  } finally {
    db?.close();
  }
}

function decodeStoredKey(payload: Buffer, options: EngineSecretOptions): string {
  let key: string;
  if (payload.subarray(0, SECURE_HEADER.length).equals(SECURE_HEADER)) {
    if (!secureStorageAvailable(options)) {
      throw new Error('desktop engine secret requires unavailable secure storage');
    }
    try {
      key = options.safeStorage.decryptString(payload.subarray(SECURE_HEADER.length));
    } catch {
      // 不把 safeStorage 原始异常透出：平台错误有时会携带钥匙串条目细节。
      throw new Error('desktop engine secret could not be decrypted');
    }
  } else if (payload.subarray(0, OWNER_ONLY_HEADER.length).equals(OWNER_ONLY_HEADER)) {
    key = payload.subarray(OWNER_ONLY_HEADER.length).toString('utf8').trimEnd();
  } else {
    throw new Error('desktop engine secret has an unknown format');
  }
  if (!isFernetKey(key)) throw new Error('desktop engine secret is invalid');
  return key;
}

/**
 * 读取或首次生成每安装 Fernet key。已有文件无论为何失败都不会被覆盖。
 */
export function loadOrCreateEngineEncryptionKey(options: EngineSecretOptions): string {
  const directory = join(options.dataDir, 'secrets');
  const path = secretPath(options.dataDir);
  ensurePrivateDirectory(directory);

  if (existsSync(path)) {
    assertRegularSecretFile(path);
    return decodeStoredKey(readFileSync(path), options);
  }

  // 升级保护：旧 Desktop 依赖后端公开 dev secret 派生 Fernet key。若先创建
  // 随机 key 再启动，所有既有凭据会在表面上仍存在、实际却永远解不开。
  // 这里只在首次建 key 时审计；发现密文则保留 DB 原状并阻止启动，等专用的
  // 事务性重加密流程处理。显式 POLARIS_ENCRYPTION_KEY 在调用本函数前已短路，
  // 因而仍给管理员留下用旧 key 启动和导出/清理凭据的恢复通道。
  const databasePath = join(options.dataDir, 'engine', 'polaris.db');
  if (databaseHasLegacyEncryptedSecrets(databasePath)) {
    throw new Error(
      `${LEGACY_ENGINE_SECRETS_ERROR}: existing Desktop credentials still use the legacy key; `
      + 'the database was left unchanged. Restore the previous key with '
      + 'POLARIS_ENCRYPTION_KEY for recovery, or upgrade with a credential re-encryption tool.',
    );
  }

  const key = generateFernetKey();
  const payload = secureStorageAvailable(options)
    ? Buffer.concat([SECURE_HEADER, options.safeStorage.encryptString(key)])
    : Buffer.concat([OWNER_ONLY_HEADER, Buffer.from(`${key}\n`, 'utf8')]);
  atomicWriteSecret(path, payload);
  return key;
}

/** 显式 env 供开发/运维覆盖；同样严格校验，绝不退回公开默认值。 */
export function configuredEngineEncryptionKey(env: NodeJS.ProcessEnv = process.env): string | null {
  const value = env.POLARIS_ENCRYPTION_KEY?.trim();
  if (!value) return null;
  if (!isFernetKey(value)) {
    throw new Error('POLARIS_ENCRYPTION_KEY is not a valid Fernet key');
  }
  return value;
}

/**
 * 只在 spawn 动作期间把 key 放进父进程 env；action 结束后精确恢复原值。
 * legacy-engine 的 command 模式在 action 内同步 spawn，子进程已经拿到副本。
 */
export async function withEngineEncryptionKey<T>(
  key: string,
  action: () => Promise<T>,
  env: NodeJS.ProcessEnv = process.env,
): Promise<T> {
  if (!isFernetKey(key)) throw new Error('desktop engine encryption key is invalid');
  const previous = env.POLARIS_ENCRYPTION_KEY;
  env.POLARIS_ENCRYPTION_KEY = key;
  try {
    return await action();
  } finally {
    if (previous === undefined) delete env.POLARIS_ENCRYPTION_KEY;
    else env.POLARIS_ENCRYPTION_KEY = previous;
  }
}
