import type { UploadFile } from "antd";
import type { IAgentScopeRuntimeWebUIInputData } from "../AgentScopeRuntimeWebUI/core/types";
import {
  HTML_ANNOTATION_MAX_BUNDLE_BYTES,
  HTML_ANNOTATION_MAX_COUNT,
  HTML_ANNOTATION_SCHEMA_VERSION,
  type DocumentAnnotationsRequest,
  type HtmlAnnotationBundle,
} from "./types";

const MAX_ATTACHMENT_URL_CHARS = 4096;
const HTML_ANNOTATION_ATTACHMENT_UID_PREFIX = "html-annotation-";

const safeBaseName = (fileName: string) => {
  const withoutExtension = fileName.replace(/\.html?$/i, "") || "document";
  return withoutExtension.replace(/[^\p{L}\p{N}_.-]+/gu, "-").slice(0, 120);
};

function buildDocumentAnnotations(
  bundle: HtmlAnnotationBundle,
  attachmentUrl: string,
): DocumentAnnotationsRequest {
  const baseName = safeBaseName(bundle.fileName);
  return {
    schema_version: HTML_ANNOTATION_SCHEMA_VERSION,
    source: {
      attachment_url: attachmentUrl,
      file_name: `${baseName}.annotation-source.html`,
      sha256: bundle.sourceSha256,
    },
    output: {
      mode: "new_file",
      format: "html",
      preserve_original: true,
      suggested_name: `${baseName}-revised.html`,
    },
    annotations: bundle.annotations,
  };
}

export function getHtmlAnnotationBundleValidationError(
  bundle: HtmlAnnotationBundle,
): string | null {
  if (bundle.annotations.length > HTML_ANNOTATION_MAX_COUNT) {
    return `单次最多添加 ${HTML_ANNOTATION_MAX_COUNT} 条批注，请发送后继续批注。`;
  }
  const worstCaseRequest = buildDocumentAnnotations(
    bundle,
    "x".repeat(MAX_ATTACHMENT_URL_CHARS),
  );
  const byteLength = new TextEncoder().encode(
    JSON.stringify(worstCaseRequest),
  ).byteLength;
  if (byteLength > HTML_ANNOTATION_MAX_BUNDLE_BYTES) {
    return "批注内容过大，请减少批注数量或缩短批注内容后重试。";
  }
  return null;
}

export async function prepareHtmlAnnotationExecutionMode(
  input: IAgentScopeRuntimeWebUIInputData,
  options: {
    planModeEnabled: boolean;
    persistPlanMode: (enabled: boolean) => Promise<void>;
    goalModeEnabled: boolean;
    setGoalModeEnabled: (enabled: boolean) => void;
  },
): Promise<IAgentScopeRuntimeWebUIInputData> {
  if (options.planModeEnabled) {
    await options.persistPlanMode(false);
  }
  if (options.goalModeEnabled) {
    options.setGoalModeEnabled(false);
  }
  return {
    ...input,
    biz_params: {
      ...(input.biz_params || {}),
      mode: "normal",
      goal_mode_enabled: false,
    },
  };
}

export function discardStaleHtmlAnnotationSubmission(
  input: IAgentScopeRuntimeWebUIInputData,
): IAgentScopeRuntimeWebUIInputData {
  const remainingBizParams = Object.fromEntries(
    Object.entries(input.biz_params || {}).filter(
      ([key]) => key !== "document_annotations",
    ),
  );
  return {
    ...input,
    fileList: input.fileList?.filter(
      (file) =>
        !String(file.uid).startsWith(HTML_ANNOTATION_ATTACHMENT_UID_PREFIX),
    ),
    biz_params:
      input.biz_params && Object.keys(remainingBizParams).length > 0
        ? remainingBizParams
        : undefined,
  };
}

export async function prepareHtmlAnnotationSubmit(
  input: IAgentScopeRuntimeWebUIInputData,
  bundle: HtmlAnnotationBundle,
  api: {
    uploadFile: (file: File) => Promise<{ url: string }>;
    filePreviewUrl: (url: string) => string;
  },
): Promise<IAgentScopeRuntimeWebUIInputData> {
  const validationError = getHtmlAnnotationBundleValidationError(bundle);
  if (validationError) throw new Error(validationError);
  const baseName = safeBaseName(bundle.fileName);
  const sourceName = `${baseName}.annotation-source.html`;
  const sourceFile = new File([bundle.canonicalHtml], sourceName, {
    type: "text/html",
  });
  const upload = await api.uploadFile(sourceFile);
  const attachmentUrl = api.filePreviewUrl(upload.url);
  const fileItem: UploadFile = {
    uid: `${HTML_ANNOTATION_ATTACHMENT_UID_PREFIX}${bundle.token}`,
    name: sourceName,
    size: sourceFile.size,
    type: sourceFile.type,
    status: "done",
    originFileObj: sourceFile as UploadFile["originFileObj"],
    response: { url: attachmentUrl },
  };
  const documentAnnotations = buildDocumentAnnotations(bundle, attachmentUrl);
  return {
    ...input,
    query:
      input.query.trim() ||
      `请根据 ${bundle.annotations.length} 条页面批注生成修改后的 HTML。`,
    fileList: [...(input.fileList || []), fileItem],
    biz_params: {
      ...(input.biz_params || {}),
      document_annotations: documentAnnotations,
    },
  };
}
