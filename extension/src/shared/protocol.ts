export interface ToolCallMsg {
  type: "tool_call";
  call_id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export interface ToolResultMsg {
  type: "tool_result";
  call_id: string;
  output: string;
  is_error: boolean;
}

/**
 * Frames pushed by the server over SSE.
 *
 * Intermediate steps (reasoning, subagent lifecycle, tool execution) are
 * DISTINCT frame types rather than being folded into the assistant text.
 * That is what lets the panel render each step as its own message instead of
 * concatenating a whole turn into one bubble.
 */
export type ServerMsg =
  | ToolCallMsg
  | { type: "connected"; session_id: string }
  | { type: "turn_started" }
  | { type: "turn_finished"; text: string }
  | { type: "assistant_delta"; text: string }
  | { type: "assistant_message"; text: string }
  | { type: "reasoning"; text: string }
  | { type: "subagent_started"; name: string; model: string; description: string }
  | {
      type: "subagent_completed";
      name: string;
      duration_ms: number;
      total_tokens: number;
      tool_calls: number;
    }
  | { type: "subagent_failed"; name: string; error: string }
  | { type: "tool_started"; name: string; arguments: Record<string, unknown> }
  | { type: "tool_finished"; name: string; success: boolean; error: string }
  | { type: "skill_invoked"; name: string }
  | { type: "usage"; input_tokens: number; output_tokens: number }
  | { type: "error"; message: string }
  | { type: "pong" };

export type PanelMsg =
  | { type: "status"; connected: boolean; sessionId: string }
  | { type: "activity"; tool: string; detail: string }
  | { type: "delta"; text: string }
  | { type: "final"; text: string }
  | { type: "started" }
  | { type: "error"; message: string }
  /** One self-contained step bubble: reasoning, subagent, tool or skill. */
  | {
      type: "step";
      kind: "reasoning" | "subagent" | "subagent_done" | "tool" | "tool_done" | "skill" | "usage";
      title: string;
      detail?: string;
      ok?: boolean;
    };
