export class ApiError extends Error {
  constructor(public status: number, public detail: string) {
    super(detail);
  }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch('/console' + path, {
    credentials: 'same-origin',
    ...options,
    headers: { 'Content-Type': 'application/json', 'X-CloudSRE-Request': 'console', ...options.headers },
  });
  if (!response.ok) {
    let detail = `请求失败（${response.status}）`;
    try {
      const body = await response.json();
      if (typeof body.detail === 'string') detail = body.detail;
    } catch { /* A transport or gateway error may not have a JSON body. */ }
    throw new ApiError(response.status, detail);
  }
  return response.json() as Promise<T>;
}

export function post<T>(path: string, body: unknown): Promise<T> {
  return api<T>(path, { method: 'POST', body: JSON.stringify(body) });
}

export type Control = {
  namespace: string; execution_mode: 'manual' | 'auto'; requested_at: string;
  stop_requested_at: string | null; stop_reason: string | null;
};
export type Incident = {
  id: string; title: string; service: string; status: string; created_at: string;
  control?: Control | null;
  latest?: { run_id: string; updated_at: string; namespace?: string; outcome?: string; error?: string } | null;
};
export type Intent = {
  id: string; action: string; resource: string; value: unknown;
  before: { uid: string; resource_version: string; spec_hash: string };
  patch: Array<{ op: string; path: string; value: unknown }>;
};
export type Quality = {
  passed: boolean; duration_seconds: number; load_requests: number; business_checks: number;
  errors: number; p95_seconds: number | null; p95_limit_seconds: number | null;
  checks: Record<string, boolean>; interrupted?: boolean;
  sampled_peak_container_memory_bytes?: Record<string, number>;
};
export type Journal = {
  run_id: string; started_at: string; outcome: string; namespace: string;
  answer?: string; proposal?: { reason: string; evidence_ids: string[] }; action_intent?: Intent;
  action_result?: { state: string; reconciled?: boolean }; error?: string;
  quality?: Quality; verification_attempts?: number;
  stop_action_observation?: string; cancellation_note?: string;
  versions?: { git_commit: string; holmes_commit: string; source_bundle_sha256: string };
  phase_seconds?: Record<string, number>;
  usage?: {
    http_attempts: number; known_usage_calls: number; unknown_usage_calls: number;
    known_token_subtotal: { total_tokens: number; prompt_tokens: number; completion_tokens: number };
    cost_state: string; cost_amount: number | null; currency?: string | null;
  };
  ticket?: { id: string; status: string; reason: string };
};
export type Evidence = { id: string; kind: string; collected_at?: string; source?: string; sha256: string; content?: unknown };
export type Detail = {
  incident: Incident; control: Control | null; namespace?: string | null;
  latest: { id: string; phase: string; updated_at: string; payload: Journal } | null;
  runs: Array<{ id: string; phase: string; payload: Journal }>;
  timeline: Array<{ id: string; at: string; phase?: string; kind: string; operation?: string; actor?: string; payload?: unknown }>;
  operator_events: Array<{ id: string; kind: string; actor: string; at: string; payload: unknown }>;
  plan_hash: string | null; decision: { decision: string; reason: string; actor: string; created_at: string } | null;
  verification_claim: string;
  verification_live?: { load_requests: number; business_checks: number; errors: number; started_at?: string; samples: Array<{ at: string; passed: boolean; http_status?: number; latency_seconds?: number }> } | null;
};

const messages: Record<string, string> = {
  'Console session required': '会话已失效，请重新输入控制台访问码。',
  'Invalid console access code': '访问码不正确，请核对本机控制台配置。',
  'Console access is not configured': '控制台尚未配置访问码，请先运行启动脚本。',
  'Too many login attempts; retry in one minute': '尝试次数较多，请一分钟后再试。',
  'Plan identity or version changed; reload the plan': '方案版本已变化，请刷新并重新核对。',
  'Task is not waiting for this plan': '任务已不在待审批状态，请查看最新进度。',
  'A stopped task cannot be approved': '任务已申请停止，无法继续批准。',
  'An immutable decision already exists': '该方案已有决定，不能改写；请查看操作记录。',
  'Task already finished': '任务已结束，请查看最终结果。',
  'This historical event has no console task': '这是历史记录，没有可停止的控制台任务。',
};
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return messages[error.detail] ?? `操作未完成（${error.status}）：${error.detail}`;
  return '无法连接控制台服务。已保留当前画面，请检查服务后重试。';
}
