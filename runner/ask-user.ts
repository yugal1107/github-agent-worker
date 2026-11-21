import { Type } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
  let posted = false;
  pi.registerTool({
    name: "ask_user",
    label: "Ask the issue requester",
    description: "Post one batch of numbered questions on the current issue and pause. Call this tool alone as the last action of the turn.",
    promptGuidelines: ["Call ask_user alone: include every current question in one call, with no parallel tool calls."],
    parameters: Type.Object({
      questions: Type.Array(Type.String({ minLength: 1, maxLength: 600 }), { minItems: 1, maxItems: 8 }),
    }),
    executionMode: "sequential",
    async execute(_id, { questions }, signal) {
      if (posted) {
        return { content: [{ type: "text", text: "Questions already posted; wait for the answer." }],
          details: { waiting: true }, terminate: true };
      }
      const { AGENT_ISSUE, AGENT_REPOSITORY, AGENT_MENTION } = process.env;
      if (!AGENT_ISSUE || !AGENT_REPOSITORY || !AGENT_MENTION) throw new Error("Missing issue context");
      const cleaned = questions.map(q => q.replace(/[\r\n]+/g, " ").trim());
      if (cleaned.some(q => !q)) throw new Error("Questions must contain text");
      const body = [
        "I need your input before I continue:", "",
        ...cleaned.map((question, i) => `${i + 1}. ${question}`), "",
        `Reply with \`${AGENT_MENTION} reply\` followed by your numbered answers.`,
      ].join("\n");
      const result = await pi.exec("gh", ["issue", "comment", AGENT_ISSUE, "--repo", AGENT_REPOSITORY, "--body", body],
        { signal, timeout: 30_000, maxBuffer: 64 * 1024 });
      if (result.code !== 0) throw new Error("Could not post questions to GitHub. Check Issues write permission.");
      posted = true;
      return { content: [{ type: "text", text: "Posted questions. Wait for the authorized reply." }],
        details: { waiting: true }, terminate: true };
    },
  });

  // If the model returned a mixed tool batch, stop before a follow-up provider call.
  // The turn_end hook runs after the assistant and tool results are persisted.
  pi.on("turn_end", (_event, ctx) => {
    if (posted) ctx.abort();
  });
}
