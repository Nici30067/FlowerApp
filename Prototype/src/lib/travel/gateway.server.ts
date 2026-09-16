/**
 * Minimal streamed call to the Lovable AI Gateway Responses API.
 * Streaming is required: reasoning models can run for minutes and a buffered
 * request would be severed by the platform. We consume the stream server-side
 * and return the final JSON object.
 */
export interface JsonCallArgs {
  system: string;
  input: string;
  schemaName: string;
  schema: Record<string, unknown>;
  signal?: AbortSignal;
}

export class GatewayError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "GatewayError";
  }
}

export async function callGatewayJson<T>(args: JsonCallArgs): Promise<T> {
  const key = process.env["LOVABLE_API_KEY"];
  if (!key) throw new GatewayError(401, "AI is not configured for this app.");

  const res = await fetch("https://ai.gateway.lovable.dev/v1/responses", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Lovable-API-Key": key,
      "X-Lovable-AIG-SDK": "fetch",
    },
    signal: args.signal ?? null,
    body: JSON.stringify({
      model: "openai/gpt-6-astra",
      stream: true,
      store: false,
      reasoning: { effort: "low", summary: "auto" },
      instructions: args.system,
      input: [
        {
          role: "user",
          content: [{ type: "input_text", text: args.input }],
        },
      ],
      text: {
        format: {
          type: "json_schema",
          name: args.schemaName,
          strict: true,
          schema: args.schema,
        },
      },
    }),
  });

  if (!res.ok || !res.body) {
    const body = await res.text().catch(() => "");
    let message = body.slice(0, 400) || res.statusText;
    try {
      const parsed = JSON.parse(body) as { error?: { message?: string }; message?: string };
      message = parsed.error?.message ?? parsed.message ?? message;
    } catch {
      /* keep raw text */
    }
    throw new GatewayError(res.status, message);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let text = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (!line.startsWith("data:")) continue;
      const payload = line.slice(5).trim();
      if (!payload || payload === "[DONE]") continue;
      try {
        const evt = JSON.parse(payload) as {
          type?: string;
          delta?: string;
          response?: { output_text?: string };
        };
        if (evt.type === "response.output_text.delta" && typeof evt.delta === "string") {
          text += evt.delta;
        } else if (evt.type === "response.completed" && evt.response?.output_text) {
          text = evt.response.output_text;
        }
      } catch {
        /* ignore non-JSON keepalives */
      }
    }
  }

  if (!text.trim()) throw new GatewayError(502, "The AI returned an empty answer.");
  return JSON.parse(text) as T;
}
