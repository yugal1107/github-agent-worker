// Deterministic provider: no network, model key, or paid usage.
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";

export default function (pi) {
  pi.registerProvider("worker-test", {
    name: "Worker test",
    baseUrl: "http://127.0.0.1:9",
    apiKey: "test-only",
    api: "worker-test-api",
    models: [{ type: "chat", id: "test", name: "Test", reasoning: false, input: ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 20000, maxTokens: 1000 }],
    streamSimple(model, context) {
      const stream = createAssistantMessageEventStream();
      const hasQuestion = context.messages.some(m => m.role === "toolResult" && m.toolName === "ask_user");
      const block = hasQuestion
        ? { type: "text", text: "Resumed with question history." }
        : { type: "toolCall", id: "question_1", name: "ask_user", arguments: { questions: ["A or B?", "Retain C?"] } };
      const message = { role: "assistant", content: [block], api: model.api, provider: model.provider, model: model.id,
        timestamp: Date.now(), stopReason: hasQuestion ? "stop" : "toolUse",
        usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
      stream.push({ type: "start", partial: message });
      if (block.type === "text") {
        stream.push({ type: "text_start", contentIndex: 0, partial: message });
        stream.push({ type: "text_end", contentIndex: 0, content: block.text, partial: message });
      } else {
        stream.push({ type: "toolcall_start", contentIndex: 0, partial: message });
        stream.push({ type: "toolcall_end", contentIndex: 0, toolCall: block, partial: message });
      }
      stream.push({ type: "done", reason: message.stopReason, message });
      stream.end();
      return stream;
    },
  });
}
