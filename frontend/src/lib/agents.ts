import { HttpAgent } from "@ag-ui/client";

const RUN_PATH = "/api/copilotkit/agent/default/run";
const DIRECT_PATH = "/backend-agui";

/** CopilotKit runtime 経由の AG-UI run endpoint（既定の transport）。 */
export function copilotKitRunUrl(): string {
  return process.env.NEXT_PUBLIC_COPILOTKIT_RUN_URL ?? RUN_PATH;
}

/** CopilotKit runtime を経由せず backend /agui へ直接つなぐ URL（フォールバック）。 */
export function aguiDirectUrl(): string {
  return DIRECT_PATH;
}

export interface CreateAguiAgentOptions {
  agentUrl?: string;
  headers?: Record<string, string>;
}

/**
 * AG-UI HttpAgent ファクトリ。既定では CopilotKit runtime route
 * （POST /api/copilotkit/agent/default/run）を指す。AG-UI events は
 * CopilotKit runtime route を通って届く（AGENTS.md 準拠）。
 * HITL の確定は AG-UI resume ではなく REST `POST /backend/approvals/{id}`。
 */
export function createAguiAgent(options: CreateAguiAgentOptions = {}): HttpAgent {
  return new HttpAgent({
    url: options.agentUrl ?? copilotKitRunUrl(),
    headers: { Accept: "text/event-stream", ...(options.headers ?? {}) },
  });
}
