export type SessionCreateResponse = {
  session_id: string;
  created_at: string;
};

export type UploadedFileProfile = {
  file_name: string;
  table_name: string;
  rows: number;
  columns: number;
  profile_path: string;
  cleaned?: boolean;
  cleaning_summary?: {
    rows_before?: number;
    rows_after?: number;
    duplicates_removed?: number;
    missing_values_before?: number;
    missing_values_after?: number;
    numeric_columns_converted?: string[];
    filled_missing_by_column?: Record<string, number>;
    outliers_clipped_by_column?: Record<string, number>;
    cleaning_applied?: boolean;
    [key: string]: unknown;
  };
};

export type UploadFilesResponse = {
  session_id: string;
  uploaded_files: UploadedFileProfile[];
};

export type SchemaColumn = {
  name: string;
  inferred_dtype: string;
  null_count: number;
  null_pct: number;
  unique_count: number;
  sample_values: string[];
  numeric_stats: Record<string, number>;
};

export type FileSchemaProfile = {
  file_name: string;
  table_name: string;
  rows: number;
  columns: number;
  columns_profile: SchemaColumn[];
  quality_summary: Record<string, number>;
  cleaned?: boolean;
  cleaning_summary?: Record<string, unknown>;
};

export type SessionSchemaResponse = {
  session_id: string;
  files: FileSchemaProfile[];
};

export type SessionQualityResponse = {
  session_id: string;
  quality_report: {
    file_count: number;
    total_rows: number;
    total_columns: number;
    total_missing_values: number;
    total_duplicate_rows: number;
    files: Array<{
      file_name: string;
      table_name: string;
      rows: number;
      columns: number;
      missing_values: number;
      duplicate_rows: number;
    }>;
  };
};

export type ChatRequest = {
  message: string;
};

export type ChartSpec = {
  chart_type?:
    | "none"
    | "pie"
    | "donut"
    | "line"
    | "area"
    | "bar"
    | "scatter"
    | "kpi"
    | "combined"
    | Array<"none" | "pie" | "donut" | "line" | "area" | "bar" | "scatter" | "kpi" | "combined">;
  type?: "kpi" | "bar" | "line" | "area" | "scatter" | "combined" | "text";
  x?: string;
  y?: string;
  title?: string | string[];
  series?: Array<Record<string, unknown>>;
  data?: Array<Record<string, unknown>> | { values?: Array<Record<string, unknown>> };
  metadata?: {
    labels?: string[];
    data?: number[];
    x_data?: number[];
    y_data?: number[];
    points?: Array<{
      x?: number | string;
      y?: number | string;
      label?: string;
    }>;
    slices?: Array<{
      label?: string;
      value?: number | string;
    }>;
    x?: string;
    y?: string;
    [key: string]: unknown;
  } | Array<{
    labels?: string[];
    data?: number[];
    x_data?: number[];
    y_data?: number[];
    points?: Array<{
      x?: number | string;
      y?: number | string;
      label?: string;
    }>;
    slices?: Array<{
      label?: string;
      value?: number | string;
    }>;
    x?: string;
    y?: string;
    [key: string]: unknown;
  }>;
  source?: string;
  reason?: string;
  format?: {
    type?: string | null;
    currency?: string | null;
    decimals?: number | null;
  };
  description?: string;
  views?: Array<{
    view?: string;
    title?: string;
    type?: string;
    encoding?: {
      text?: {
        field?: string;
        aggregate?: string;
        format?: string;
      };
      x?: {
        field?: string;
        type?: string;
        title?: string;
      };
      y?: {
        field?: string;
        type?: string;
        title?: string;
      };
      tooltip?: Array<Record<string, unknown>>;
    };
    filter?: Record<string, unknown>;
  }>;
};

export type ChatResponse = {
  session_id: string;
  question: string;
  answer_type: "text" | "table" | "text_and_table" | "text_and_chart";
  answer_text: string;
  table_preview: Array<Record<string, unknown>>;
  chart_spec: ChartSpec | null;
  follow_up_suggestions: string[];
  context_applied: boolean;
  provenance: Record<string, unknown>;
};
