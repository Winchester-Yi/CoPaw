import type { CronJobSpecOutput } from "@/api/types";
import { buildCronJobSubmitPayload } from "../Control/CronJobs/helpers";

export type CronTaskEditFormValues = Parameters<
  typeof buildCronJobSubmitPayload
>[0] & {
  taskContentText?: string;
};

export type ReplaceCronJob = (
  jobId: string,
  payload: ReturnType<typeof buildCronJobSubmitPayload>,
) => Promise<unknown>;

type TextContentPart = {
  text?: unknown;
  type?: unknown;
};

type RequestMessage = {
  content?: unknown;
  role?: unknown;
};

function parseRequestInput(input: unknown): unknown {
  if (typeof input !== "string") {
    return input;
  }

  const trimmed = input.trim();
  if (!trimmed) {
    return undefined;
  }

  try {
    return JSON.parse(trimmed);
  } catch {
    return input;
  }
}

export function extractTaskContentText(input: unknown): string {
  const parsed = parseRequestInput(input);
  const messages = Array.isArray(parsed) ? (parsed as RequestMessage[]) : [];
  const userMessage =
    messages.find((message) => message?.role === "user") || messages[0];

  if (!userMessage) {
    return typeof input === "string" ? input : "";
  }

  if (typeof userMessage.content === "string") {
    return userMessage.content;
  }

  if (Array.isArray(userMessage.content)) {
    return (userMessage.content as TextContentPart[])
      .filter((part) => part?.type === "text" && typeof part.text === "string")
      .map((part) => String(part.text))
      .join("\n");
  }

  return typeof input === "string" ? input : "";
}

export function buildRequestInputFromTaskContent(text: string) {
  return [
    {
      role: "user",
      content: [
        {
          text,
          type: "text",
        },
      ],
    },
  ];
}

export function prepareCronTaskEditValues(
  values: CronTaskEditFormValues,
): Parameters<typeof buildCronJobSubmitPayload>[0] {
  const { taskContentText, ...restValues } = values;
  if (restValues.task_type === "workflow") {
    return restValues;
  }
  if (typeof taskContentText !== "string") {
    return restValues;
  }

  if (restValues.task_type === "text") {
    return {
      ...restValues,
      text: taskContentText,
    };
  }

  return {
    ...restValues,
    request: {
      ...restValues.request,
      input: JSON.stringify(buildRequestInputFromTaskContent(taskContentText)),
    },
  };
}

export async function submitCronTaskEdit(
  task: CronJobSpecOutput,
  values: CronTaskEditFormValues,
  replaceCronJob: ReplaceCronJob,
) {
  const payload = buildCronJobSubmitPayload(prepareCronTaskEditValues(values));
  await replaceCronJob(task.id, payload);
  return payload;
}
