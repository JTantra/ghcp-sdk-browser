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

export type ServerMsg =
  | ToolCallMsg
  | { type: "connected"; session_id: string }
  | { type: "turn_started" }
  | { type: "turn_finished"; text: string }
  | { type: "assistant_delta"; text: string }
  | { type: "assistant_message"; text: string }
  | { type: "error"; message: string }
  | { type: "pong" };

export type PanelMsg =
  | { type: "status"; connected: boolean; sessionId: string }
  | { type: "activity"; tool: string; detail: string }
  | { type: "delta"; text: string }
  | { type: "final"; text: string }
  | { type: "started" }
  | { type: "error"; message: string };
