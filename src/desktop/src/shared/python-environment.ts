export interface PythonSelection {
  mode: 'managed' | 'local';
  executable?: string;
  pathDirectories: string[];
}

export interface PythonCandidate {
  executable: string;
  version: string;
  architecture: string;
  implementation: string;
  compatible: boolean;
  reason?: string;
}

export interface PythonEnvironmentStatus {
  current: PythonSelection | null;
  currentPython?: PythonCandidate;
  pending: PythonSelection | null;
  effectivePath: string[];
  jobId: string | null;
  phase: 'idle' | 'detect' | 'python' | 'venv' | 'install' | 'validate' | 'waiting' | 'switching' | 'ready' | 'failed' | 'cancelled';
  message?: string;
  activeTasks?: number;
  generation: number;
}
