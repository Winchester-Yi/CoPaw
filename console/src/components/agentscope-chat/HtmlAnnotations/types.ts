export const HTML_ANNOTATION_SCHEMA_VERSION = 1 as const;
export const HTML_ANNOTATION_MAX_COUNT = 20;
export const HTML_ANNOTATION_MAX_BUNDLE_BYTES = 128 * 1024;
export const HTML_ANNOTATION_MAX_EXCERPT_CHARS = 4096;
export const HTML_ANNOTATION_MAX_TEXT_QUOTE_CHARS = 1024;
export const HTML_ANNOTATION_MAX_SELECTOR_CHARS = 2048;
export const HTML_ANNOTATION_MAX_ATTRIBUTES = 16;
export const HTML_ANNOTATION_MAX_ATTRIBUTE_NAME_CHARS = 128;
export const HTML_ANNOTATION_MAX_ATTRIBUTE_VALUE_CHARS = 512;
export const HTML_ANNOTATION_MAX_ROLE_CHARS = 128;

export interface HtmlAnnotationTextQuote {
  exact: string;
  prefix?: string;
  suffix?: string;
}

export interface HtmlAnnotationTarget {
  runtime_generated: boolean;
  tag_name?: string;
  role?: string;
  stable_attributes?: Record<string, string>;
  selector?: string;
  sibling_index?: number;
  text_quote?: HtmlAnnotationTextQuote;
  rendered_html?: string;
  ancestor_html?: string;
  computed_style?: Record<string, string>;
}

export interface HtmlAnnotation {
  id: string;
  comment: string;
  target: HtmlAnnotationTarget;
}

export interface HtmlAnnotationBundle {
  token: string;
  chatKey: string;
  sourceKey: string;
  fileName: string;
  canonicalHtml: string;
  sourceSha256: string;
  annotations: HtmlAnnotation[];
}

export interface DocumentAnnotationsRequest {
  schema_version: typeof HTML_ANNOTATION_SCHEMA_VERSION;
  source: {
    attachment_url: string;
    file_name: string;
    sha256: string;
  };
  output: {
    mode: "new_file";
    format: "html";
    preserve_original: true;
    suggested_name: string;
  };
  annotations: HtmlAnnotation[];
}
