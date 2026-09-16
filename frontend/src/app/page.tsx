"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import ChatChart from "@/components/chat-chart";
import {
  createSession,
  getSessionSchema,
  sendChatMessage,
  uploadSessionFiles,
} from "@/services/api";
import type { ChartSpec, ChatResponse } from "@/types/api";

type UploadStatus = "idle" | "uploading" | "success" | "error";
type ChatStatus = "idle" | "sending" | "error";
type UploadedFileChip = {
  name: string;
  sizeLabel: string;
};

type DemoVideo = {
  name: string;
  sizeLabel: string;
  url: string;
};

type CleaningReport = {
  fileName: string;
  cleaned: boolean;
  summary: Record<string, unknown>;
};

function formatAnswerMarkdown(answerText: string): string {
  const text = answerText.trim();
  if (!text) {
    return "No answer content was returned.";
  }

  const trimmedToAnswer = text
    .replace(/^\s*(?:#{1,6}\s*)?(?:\*\*\s*)?key\s*findings(?:\s*\*\*)?\s*:?\s*/i, "")
    .trim();

  if (!trimmedToAnswer) {
    return "No answer content was returned.";
  }

  const hasMarkdownSyntax = /(^#{1,4}\s)|(```)|(^\s*[-*+]\s)|(^\s*\d+\.\s)|(\*\*)|(__)|(\*)/m.test(trimmedToAnswer);
  if (hasMarkdownSyntax) {
    return trimmedToAnswer;
  }

  const lines = trimmedToAnswer
    .split(/\n+/)
    .map((line) => line.trim())
    .filter(Boolean);

  if (lines.length <= 1) {
    return trimmedToAnswer;
  }

  const [first, ...rest] = lines;
  const bullets = rest.map((line) => `- ${line}`).join("\n");
  return `${first}\n\n${bullets}`;
}

function splitAnswerContent(answerText: string): {
  mainMarkdown: string;
  hiddenMarkdown: string;
} {
  const text = answerText.trim();
  if (!text) {
    return {
      mainMarkdown: "No answer content was returned.",
      hiddenMarkdown: "",
    };
  }

  const sectionHeaderPattern = /^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*(evidence|assumptions(?:\s+and\s+limitations)?|limitations|recommended\s+next\s+steps|note\s+on\s+relationships\/confidence)\s*:?\s*(?:\*\*)?\s*/gim;
  const matches = Array.from(text.matchAll(sectionHeaderPattern));

  if (!matches.length || matches[0].index === undefined) {
    return {
      mainMarkdown: formatAnswerMarkdown(text),
      hiddenMarkdown: "",
    };
  }

  const splitIndex = matches[0].index;
  const mainRaw = text.slice(0, splitIndex).trim();
  const hiddenRaw = text.slice(splitIndex).trim();

  return {
    mainMarkdown: formatAnswerMarkdown(mainRaw),
    hiddenMarkdown: hiddenRaw,
  };
}

function hasRenderableChartData(chartSpec: ChartSpec | null | undefined): boolean {
  if (!chartSpec) {
    return false;
  }

  if (Array.isArray(chartSpec.chart_type)) {
    const metadataList = Array.isArray(chartSpec.metadata) ? chartSpec.metadata : [];
    return chartSpec.chart_type.some((chartType, index) => {
      if (chartType === "none") {
        return false;
      }
      const metadata =
        index < metadataList.length && metadataList[index] && typeof metadataList[index] === "object"
          ? metadataList[index]
          : null;
      if (!metadata) {
        return false;
      }
      const labels = Array.isArray(metadata.labels) ? metadata.labels : [];
      const data = Array.isArray(metadata.data) ? metadata.data : [];
      const slices = Array.isArray(metadata.slices) ? metadata.slices : [];
      const points = Array.isArray(metadata.points) ? metadata.points : [];
      const xData = Array.isArray(metadata.x_data) ? metadata.x_data : [];
      const yData = Array.isArray(metadata.y_data) ? metadata.y_data : [];
      return (labels.length > 0 && data.length > 0) || slices.length > 0 || points.length > 0 || (xData.length > 0 && yData.length > 0);
    });
  }

  if (chartSpec.chart_type === "none") {
    return false;
  }
  if (chartSpec.metadata && typeof chartSpec.metadata === "object" && !Array.isArray(chartSpec.metadata)) {
    const labels = Array.isArray(chartSpec.metadata.labels) ? chartSpec.metadata.labels : [];
    const data = Array.isArray(chartSpec.metadata.data) ? chartSpec.metadata.data : [];
    const slices = Array.isArray(chartSpec.metadata.slices) ? chartSpec.metadata.slices : [];
    const points = Array.isArray(chartSpec.metadata.points) ? chartSpec.metadata.points : [];
    const xData = Array.isArray(chartSpec.metadata.x_data) ? chartSpec.metadata.x_data : [];
    const yData = Array.isArray(chartSpec.metadata.y_data) ? chartSpec.metadata.y_data : [];
    if ((labels.length > 0 && data.length > 0) || slices.length > 0 || points.length > 0 || (xData.length > 0 && yData.length > 0)) {
      return true;
    }
  }
  if (Array.isArray(chartSpec.series) && chartSpec.series.length > 0) {
    return true;
  }
  if (Array.isArray(chartSpec.data) && chartSpec.data.length > 0) {
    return true;
  }
  if (
    chartSpec.data &&
    typeof chartSpec.data === "object" &&
    Array.isArray(chartSpec.data.values) &&
    chartSpec.data.values.length > 0
  ) {
    return true;
  }
  return false;
}

function coerceNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string") {
    const numeric = Number(value.replace(/,/g, "").trim());
    if (Number.isFinite(numeric)) {
      return numeric;
    }
  }
  return null;
}

function formatTableCellValue(value: unknown): string {
  if (value === null) {
    return "null";
  }
  if (value === undefined) {
    return "";
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  if (Array.isArray(value) || typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function buildFallbackChartSpec(
  previewRows: Array<Record<string, unknown>>,
  question: string,
): ChartSpec | null {
  if (!previewRows.length || !previewRows[0]) {
    return null;
  }

  const keys = Object.keys(previewRows[0]);
  if (!keys.length) {
    return null;
  }

  const numericKeys = keys.filter((key) =>
    previewRows.some((row) => coerceNumber(row[key]) !== null),
  );
  if (!numericKeys.length) {
    return null;
  }

  const xKey = keys.find((key) => !numericKeys.includes(key)) || keys[0];
  const yKey = numericKeys.find((key) => key !== xKey) || numericKeys[0];
  if (!xKey || !yKey) {
    return null;
  }

  const series = previewRows
    .slice(0, 100)
    .filter((row) => row[xKey] !== undefined && coerceNumber(row[yKey]) !== null)
    .map((row) => ({
      ...row,
      [xKey]: String(row[xKey] ?? ""),
      [yKey]: coerceNumber(row[yKey]) ?? 0,
    }));

  if (!series.length) {
    return null;
  }

  const wantsDonut = /donut|doughnut/i.test(question);
  const wantsPie = !wantsDonut && /distribution|share|breakdown|composition|split|pie/i.test(question);
  const wantsArea = /area|cumulative|filled trend/i.test(question);
  const wantsScatter = /scatter|correlation|relationship/i.test(question);

  if (wantsScatter && numericKeys.length >= 2) {
    const xNumericKey = numericKeys[0];
    const yNumericKey = numericKeys[1];
    const points = previewRows
      .slice(0, 200)
      .map((row) => ({
        x: coerceNumber(row[xNumericKey]),
        y: coerceNumber(row[yNumericKey]),
      }))
      .filter((point): point is { x: number; y: number } => point.x !== null && point.y !== null);

    if (!points.length) {
      return null;
    }

    return {
      chart_type: "scatter",
      title: question || `${yNumericKey} vs ${xNumericKey}`,
      metadata: {
        x: xNumericKey,
        y: yNumericKey,
        points,
      },
    };
  }

  return {
    chart_type: wantsDonut ? "donut" : wantsPie ? "pie" : wantsArea ? "area" : "bar",
    title: question || `${yKey} by ${xKey}`,
    metadata: (wantsPie || wantsDonut)
      ? {
          labels: series.map((row) => String(row[xKey] ?? "")),
          data: series.map((row) => Number(row[yKey] ?? 0)),
          slices: series.map((row) => ({
            label: String(row[xKey] ?? ""),
            value: Number(row[yKey] ?? 0),
          })),
        }
      : {
          labels: series.map((row) => String(row[xKey] ?? "")),
          data: series.map((row) => Number(row[yKey] ?? 0)),
          x: xKey,
          y: yKey,
        },
  };
}

function formatFileSize(sizeInBytes: number): string {
  if (sizeInBytes < 1024) {
    return `${sizeInBytes} B`;
  }
  if (sizeInBytes < 1024 * 1024) {
    return `${(sizeInBytes / 1024).toFixed(1)} KB`;
  }
  return `${(sizeInBytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function Home() {
  const [sessionId, setSessionId] = useState<string>("");
  const [isWidgetOpen, setIsWidgetOpen] = useState<boolean>(false);
  const [uploadStatus, setUploadStatus] = useState<UploadStatus>("idle");
  const [uploadMessage, setUploadMessage] = useState<string>("");
  const [chatInput, setChatInput] = useState<string>("");
  const [chatStatus, setChatStatus] = useState<ChatStatus>("idle");
  const [chatError, setChatError] = useState<string>("");
  const [chatHistory, setChatHistory] = useState<ChatResponse[]>([]);
  const [pendingMessage, setPendingMessage] = useState<string>("");
  const [uploadedFiles, setUploadedFiles] = useState<UploadedFileChip[]>([]);
  const [uploadReports, setUploadReports] = useState<CleaningReport[]>([]);
  const [demoVideo, setDemoVideo] = useState<DemoVideo | null>(null);
  const [cleanBeforeUpload, setCleanBeforeUpload] = useState<boolean>(true);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const videoInputRef = useRef<HTMLInputElement | null>(null);
  const demoVideoUrlRef = useRef<string | null>(null);
  const currentDateLabel = useMemo(
    () =>
      new Intl.DateTimeFormat("en-US", {
        month: "short",
        day: "numeric",
        year: "numeric",
      }).format(new Date()),
    [],
  );

  useEffect(() => {
    demoVideoUrlRef.current = demoVideo?.url ?? null;
  }, [demoVideo]);

  useEffect(() => () => {
    if (demoVideoUrlRef.current) {
      URL.revokeObjectURL(demoVideoUrlRef.current);
    }
  }, []);

  async function ensureSession(): Promise<string> {
    if (sessionId) {
      return sessionId;
    }
    const session = await createSession();
    setSessionId(session.session_id);
    return session.session_id;
  }

  async function handleUpload(files: File[]): Promise<void> {
    if (!files.length) {
      setUploadMessage("Select at least one CSV file to upload.");
      return;
    }

    setUploadStatus("uploading");
    setUploadMessage("");

    try {
      const currentSessionId = await ensureSession();
      const upload = await uploadSessionFiles(currentSessionId, files, {
        cleanData: cleanBeforeUpload,
      });
      const schema = await getSessionSchema(currentSessionId);
      const schemaByFileName = new Map(
        schema.files.map((item) => [item.file_name, item]),
      );

      setSessionId(currentSessionId);
      setUploadStatus("success");
      const cleanedFiles = upload.uploaded_files.filter(
        (item) => (item.cleaned ?? cleanBeforeUpload) || item.cleaning_summary?.cleaning_applied === true,
      ).length;
      const modeLabel = cleanBeforeUpload
        ? `Uploaded and cleaned ${cleanedFiles}/${files.length} file(s)`
        : "Uploaded without cleaning";
      setUploadMessage(`${modeLabel}: ${files.map((file) => file.name).join(", ")}`);
      setUploadReports(
        upload.uploaded_files.map((item) => ({
          fileName: item.file_name,
          cleaned:
            schemaByFileName.get(item.file_name)?.cleaned
            ?? item.cleaned
            ?? cleanBeforeUpload,
          summary:
            schemaByFileName.get(item.file_name)?.cleaning_summary
            ?? item.cleaning_summary
            ?? {},
        })),
      );
      setUploadedFiles((prev) => {
        const deduped = new Map(prev.map((file) => [file.name, file]));
        files.forEach((file) => {
          deduped.set(file.name, {
            name: file.name,
            sizeLabel: formatFileSize(file.size),
          });
        });
        return Array.from(deduped.values());
      });
      setChatError("");
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "Unexpected upload failure";
      setUploadMessage(message);
      setUploadStatus("error");
    }
  }

  async function handleSendMessage(): Promise<void> {
    const trimmed = chatInput.trim();
    if (!trimmed) {
      setChatError("Enter a question before sending.");
      return;
    }

    setChatStatus("sending");
    setChatError("");
    setPendingMessage(trimmed);
    setChatInput("");

    try {
      const currentSessionId = await ensureSession();
      const response = await sendChatMessage(currentSessionId, { message: trimmed });
      setChatHistory((prev) => [...prev, response]);
      setSessionId(currentSessionId);
      setPendingMessage("");
      setChatStatus("idle");
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "Unexpected chat request failure";
      setChatError(message);
      setPendingMessage("");
      setChatStatus("error");
    }
  }

  async function handleStartNewChat(): Promise<void> {
    setChatHistory([]);
    setPendingMessage("");
    setChatInput("");
    setChatError("");
    setChatStatus("idle");
    setUploadStatus("idle");
    setUploadMessage("");
    setUploadedFiles([]);
    setUploadReports([]);
    setDemoVideo((currentVideo) => {
      if (currentVideo?.url) {
        URL.revokeObjectURL(currentVideo.url);
      }
      return null;
    });

    try {
      const session = await createSession();
      setSessionId(session.session_id);
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "Failed to start a new chat session";
      setSessionId("");
      setChatError(message);
    }
  }

  function handleFilePickerChange(event: React.ChangeEvent<HTMLInputElement>): void {
    const files = Array.from(event.target.files ?? []);
    if (!files.length) {
      return;
    }
    void handleUpload(files);
    event.currentTarget.value = "";
  }

  function handleVideoPickerChange(event: React.ChangeEvent<HTMLInputElement>): void {
    const selectedVideo = event.target.files?.[0];
    if (!selectedVideo) {
      return;
    }

    const nextVideoUrl = URL.createObjectURL(selectedVideo);
    if (!nextVideoUrl.startsWith("blob:")) {
      URL.revokeObjectURL(nextVideoUrl);
      event.currentTarget.value = "";
      return;
    }
    setDemoVideo((currentVideo) => {
      if (currentVideo?.url) {
        URL.revokeObjectURL(currentVideo.url);
      }
      return {
        name: selectedVideo.name,
        sizeLabel: formatFileSize(selectedVideo.size),
        url: nextVideoUrl,
      };
    });
    event.currentTarget.value = "";
  }

  function handleClearDemoVideo(): void {
    setDemoVideo((currentVideo) => {
      if (currentVideo?.url) {
        URL.revokeObjectURL(currentVideo.url);
      }
      return null;
    });
  }

  return (
    <main className="chatbot-blank-canvas">
      <input
        ref={fileInputRef}
        type="file"
        accept=".csv"
        multiple
        className="hidden-file-input"
        onChange={handleFilePickerChange}
      />
      <input
        ref={videoInputRef}
        type="file"
        accept="video/*"
        className="hidden-file-input"
        aria-hidden="true"
        onChange={handleVideoPickerChange}
      />

      <button
        type="button"
        className={isWidgetOpen ? "chat-launcher-btn chat-launcher-btn-open" : "chat-launcher-btn"}
        onClick={() => setIsWidgetOpen((prev) => !prev)}
        aria-label={isWidgetOpen ? "Close chat" : "Open chat"}
      >
        {isWidgetOpen ? (
          <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false">
            <path d="M6.4 5L12 10.6L17.6 5L19 6.4L13.4 12L19 17.6L17.6 19L12 13.4L6.4 19L5 17.6L10.6 12L5 6.4L6.4 5Z" fill="currentColor" />
          </svg>
        ) : (
          <>
            <span className="launcher-icon-wrap" aria-hidden="true">
              <svg viewBox="0 0 24 24" width="18" height="18" focusable="false">
                <path d="M4 5.5C4 4.67 4.67 4 5.5 4H18.5C19.33 4 20 4.67 20 5.5V14.5C20 15.33 19.33 16 18.5 16H9.5L5.2 19.6C4.71 20.01 4 19.66 4 19.02V16C4 15.45 4.45 15 5 15H5.5C4.67 15 4 14.33 4 13.5V5.5Z" fill="currentColor" />
              </svg>
            </span>
            <span className="launcher-label">Chat</span>
          </>
        )}
      </button>

      {isWidgetOpen && (
        <section className="chat-widget-panel">
          <header className="chat-widget-header">
            <div className="chat-header-main">
              <div className="chatbot-avatar-badge" aria-hidden="true">
                <svg viewBox="0 0 24 24" width="18" height="18" focusable="false">
                  <path d="M12 2C13.1 2 14 2.9 14 4V5H16C17.66 5 19 6.34 19 8V14C19 15.66 17.66 17 16 17H8C6.34 17 5 15.66 5 14V8C5 6.34 6.34 5 8 5H10V4C10 2.9 10.9 2 12 2ZM9 10C8.45 10 8 10.45 8 11C8 11.55 8.45 12 9 12C9.55 12 10 11.55 10 11C10 10.45 9.55 10 9 10ZM15 10C14.45 10 14 10.45 14 11C14 11.55 14.45 12 15 12C15.55 12 16 11.55 16 11C16 10.45 15.55 10 15 10ZM8.8 13.8C9.5 14.53 10.67 15 12 15C13.33 15 14.5 14.53 15.2 13.8L16.25 14.75C15.27 15.78 13.72 16.4 12 16.4C10.28 16.4 8.73 15.78 7.75 14.75L8.8 13.8Z" fill="currentColor" />
                </svg>
              </div>

              <div className="chat-header-copy">
                <h2>Analytics Chatbot</h2>
                <div className="chat-status-row">
                  <span className="status-dot" aria-hidden="true" />
                  <span className="status-text">Online now</span>
                  {sessionId && <span className="session-badge">Session active</span>}
                </div>
              </div>
            </div>

            <div className="chat-header-actions">
              <button
                type="button"
                className="header-icon-btn"
                aria-label="Start new chat"
                title="Start new chat"
                onClick={() => {
                  void handleStartNewChat();
                }}
                disabled={chatStatus === "sending" || uploadStatus === "uploading"}
              >
                <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false">
                  <path d="M12 5V2L8 6L12 10V7C14.76 7 17 9.24 17 12C17 14.76 14.76 17 12 17C9.24 17 7 14.76 7 12H5C5 15.87 8.13 19 12 19C15.87 19 19 15.87 19 12C19 8.13 15.87 5 12 5Z" fill="currentColor" />
                </svg>
              </button>

              <button
                type="button"
                className="header-icon-btn"
                aria-label="Close chat"
                title="Close chat"
                onClick={() => setIsWidgetOpen(false)}
              >
                <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false">
                  <path d="M6.4 5L12 10.6L17.6 5L19 6.4L13.4 12L19 17.6L17.6 19L12 13.4L6.4 19L5 17.6L10.6 12L5 6.4L6.4 5Z" fill="currentColor" />
                </svg>
              </button>
            </div>
          </header>

          <div className="chat-widget-messages">
            <section className="chat-intro-header" aria-label="Assistant intro">
              <span className="chat-intro-icon" aria-hidden="true">
                <svg viewBox="0 0 24 24" width="16" height="16" focusable="false">
                  <path d="M4 5.5C4 4.67 4.67 4 5.5 4H18.5C19.33 4 20 4.67 20 5.5V13.5C20 14.33 19.33 15 18.5 15H12L8 18.2V15H5.5C4.67 15 4 14.33 4 13.5V5.5Z" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                  <path d="M7 7.5H17M7 10.5H13" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                  <path d="M7 3V4M12 3V4M17 3V4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                </svg>
              </span>
              <span className="chat-intro-copy">
                <strong>CSV Insights Assistant</strong>
                <small>Ask questions across your uploaded CSV files</small>
              </span>
            </section>

            <div className="chat-date-divider" aria-label="Conversation date">
              TODAY · {currentDateLabel}
            </div>

            {demoVideo && (
              <section className="demo-video-card" aria-label="Working demo video">
                <div className="demo-video-header">
                  <div>
                    <strong>Working demo video</strong>
                    <p>{demoVideo.name} · {demoVideo.sizeLabel}</p>
                  </div>
                  <button
                    type="button"
                    className="demo-video-clear-btn"
                    onClick={handleClearDemoVideo}
                  >
                    Remove
                  </button>
                </div>
                <video
                  className="demo-video-player"
                  controls
                  preload="metadata"
                  aria-label="Working demo video preview"
                  src={demoVideo.url}
                />
              </section>
            )}

            {uploadedFiles.length > 0 && (
              <section className="uploaded-files-strip" aria-label="Uploaded files">
                {uploadedFiles.map((file) => (
                  <article key={file.name} className="uploaded-file-chip">
                    <span className="uploaded-file-icon" aria-hidden="true">
                      <svg viewBox="0 0 24 24" width="16" height="16" focusable="false">
                        <path d="M6 2H14L20 8V20C20 21.1 19.1 22 18 22H6C4.9 22 4 21.1 4 20V4C4 2.9 4.9 2 6 2ZM13 3.5V9H18.5" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
                        <path d="M8 14H16M8 17H13" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
                      </svg>
                    </span>
                    <span className="uploaded-file-meta">
                      <span className="uploaded-file-name">{file.name}</span>
                      <span className="uploaded-file-size">{file.sizeLabel}</span>
                    </span>
                  </article>
                ))}
              </section>
            )}

            {uploadReports.length > 0 && (
              <section className="upload-details-inline-wrap" aria-label="Cleaning details panel">
                <details className="upload-details-card upload-details-inline-card">
                  <summary className="upload-details-summary upload-details-chip-summary">
                    <span>Show cleaning details</span>
                    <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" focusable="false">
                      <path d="M7 10L12 15L17 10" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                  </summary>
                  <div className="upload-details-body">
                    {uploadReports.map((report) => {
                      const duplicatesRemoved = typeof report.summary.duplicates_removed === "number"
                        ? report.summary.duplicates_removed
                        : null;
                      const missingBefore = typeof report.summary.missing_values_before === "number"
                        ? report.summary.missing_values_before
                        : null;
                      const missingAfter = typeof report.summary.missing_values_after === "number"
                        ? report.summary.missing_values_after
                        : null;
                      const converted = Array.isArray(report.summary.numeric_columns_converted)
                        ? report.summary.numeric_columns_converted.map((item) => String(item))
                        : [];
                      const filledMissingColumns = report.summary.filled_missing_by_column && typeof report.summary.filled_missing_by_column === "object"
                        ? Object.entries(report.summary.filled_missing_by_column as Record<string, unknown>)
                        : [];
                      const outlierColumns = report.summary.outliers_clipped_by_column && typeof report.summary.outliers_clipped_by_column === "object"
                        ? Object.entries(report.summary.outliers_clipped_by_column as Record<string, unknown>)
                        : [];
                      const verifiedBefore = report.summary.verified_quality_before && typeof report.summary.verified_quality_before === "object"
                        ? (report.summary.verified_quality_before as Record<string, unknown>)
                        : null;
                      const verifiedAfter = report.summary.verified_quality_after && typeof report.summary.verified_quality_after === "object"
                        ? (report.summary.verified_quality_after as Record<string, unknown>)
                        : null;
                      const hasStructuredSummary = Object.keys(report.summary).length > 0;

                      return (
                        <article key={report.fileName} className="upload-details-item">
                          <p className="upload-details-title">
                            {report.fileName}
                            {report.cleaned ? " - cleaned" : " - uploaded without cleaning"}
                          </p>
                          {report.cleaned ? (
                            hasStructuredSummary ? (
                              <ul className="upload-details-list">
                                <li>Duplicates removed: {duplicatesRemoved ?? "unknown"}</li>
                                <li>
                                  Missing values: {missingBefore ?? "unknown"}
                                  {" -> "}
                                  {missingAfter ?? "unknown"}
                                </li>
                                <li>
                                  Filled missing (by column): {filledMissingColumns.length > 0
                                    ? filledMissingColumns.map(([key, value]) => `${key} (${String(value)})`).join(", ")
                                    : "none"}
                                </li>
                                <li>
                                  Numeric conversions: {converted.length > 0 ? converted.join(", ") : "none"}
                                </li>
                                <li>
                                  Outlier clipping: {outlierColumns.length > 0
                                    ? outlierColumns.map(([key, value]) => `${key} (${String(value)})`).join(", ")
                                    : "none"}
                                </li>
                                {verifiedBefore && verifiedAfter && (
                                  <>
                                    <li>
                                      Verified missing values: {String(verifiedBefore.missing_values ?? "unknown")}
                                      {" -> "}
                                      {String(verifiedAfter.missing_values ?? "unknown")}
                                    </li>
                                    <li>
                                      Verified duplicate rows: {String(verifiedBefore.duplicate_rows ?? "unknown")}
                                      {" -> "}
                                      {String(verifiedAfter.duplicate_rows ?? "unknown")}
                                    </li>
                                  </>
                                )}
                              </ul>
                            ) : (
                              <p className="upload-details-note">
                                Cleaning ran, but no detailed stats were returned by backend for this upload.
                              </p>
                            )
                          ) : (
                            <p className="upload-details-note">Cleaning disabled for this file.</p>
                          )}
                        </article>
                      );
                    })}
                  </div>
                </details>
              </section>
            )}

            <article className="message-row assistant-row assistant-intro-row">
              <div className="message-bubble assistant-bubble">
                <p>
                  Hello, how can I help you? I can answer questions based on uploaded CSV files.
                </p>
              </div>
            </article>

            {chatHistory.map((item, idx) => {
              const previewRows = Array.isArray(item.table_preview) ? item.table_preview : [];
              const { mainMarkdown, hiddenMarkdown } = splitAnswerContent(item.answer_text);
              const detailTableColumns = previewRows.length > 0
                ? Array.from(new Set(previewRows.flatMap((row) => Object.keys(row ?? {}))))
                : [];
              const hasDetailSection = Boolean(hiddenMarkdown) || detailTableColumns.length > 0;
              const fallbackChartSpec =
                item.answer_type === "text_and_chart" && !hasRenderableChartData(item.chart_spec)
                  ? buildFallbackChartSpec(previewRows, item.question)
                  : null;
              const resolvedChartSpec = hasRenderableChartData(item.chart_spec)
                ? item.chart_spec
                : fallbackChartSpec;

              return (
                <article key={`${item.question}-${idx}`} className="chat-thread-block">
                  <div className="message-row user-row">
                    <div className="message-bubble user-bubble">
                      <p>{item.question}</p>
                    </div>
                  </div>

                  <div className="message-row assistant-row">
                    <div className="message-bubble assistant-bubble">
                      <div className="assistant-meta">
                        <span>Assistant</span>
                        <span className="response-type">{item.answer_type}</span>
                      </div>

                      <div className="answer-markdown">
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>
                          {mainMarkdown}
                        </ReactMarkdown>
                      </div>

                      {hasDetailSection && (
                        <details className="answer-process-card">
                          <summary className="answer-process-summary">Show details</summary>
                          <div className="answer-process-content">
                            {hiddenMarkdown && (
                              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                                {hiddenMarkdown}
                              </ReactMarkdown>
                            )}

                            {detailTableColumns.length > 0 && (
                              <div className="details-table-wrap">
                                <table className="details-table">
                                  <thead>
                                    <tr>
                                      {detailTableColumns.map((col) => (
                                        <th key={col} className="details-th">{col}</th>
                                      ))}
                                    </tr>
                                  </thead>
                                  <tbody>
                                    {previewRows.map((row, rowIdx) => (
                                      <tr key={rowIdx}>
                                        {detailTableColumns.map((col) => (
                                          <td key={`${col}-${rowIdx}`} className="details-td">
                                            {formatTableCellValue(row[col])}
                                          </td>
                                        ))}
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              </div>
                            )}
                          </div>
                        </details>
                      )}

                      {resolvedChartSpec && <ChatChart chartSpec={resolvedChartSpec} />}
                    </div>
                  </div>
                </article>
              );
            })}

            {pendingMessage && (
              <article className="chat-thread-block">
                <div className="message-row user-row">
                  <div className="message-bubble user-bubble pending-user-bubble">
                    <p>{pendingMessage}</p>
                  </div>
                </div>
              </article>
            )}

            {chatStatus === "sending" && (
              <article className="message-row assistant-row">
                <div className="message-bubble assistant-bubble typing-bubble" aria-live="polite">
                  <div className="assistant-meta">
                    <span>Assistant</span>
                    <span className="response-type">thinking</span>
                  </div>
                  <div className="typing-indicator" aria-hidden="true">
                    <span className="typing-dot" />
                    <span className="typing-dot" />
                    <span className="typing-dot" />
                  </div>
                  <p className="typing-label">Analyzing your data...</p>
                </div>
              </article>
            )}
          </div>

          {(chatError || uploadMessage) && (
            <div className="chat-status-strip">
              {chatError && <p className="chat-error-msg">{chatError}</p>}
              {uploadMessage && (
                <p className={uploadStatus === "error" ? "chat-error-msg" : "chat-ok-msg"}>{uploadMessage}</p>
              )}
            </div>
          )}

          <div className="chat-widget-composer">
            <div className="chat-toolbar-row">
              <div className="chat-toolbar-left">
                <label className="clean-toggle" title="Apply basic data cleaning before analytics">
                  <input
                    type="checkbox"
                    checked={cleanBeforeUpload}
                    onChange={(event) => setCleanBeforeUpload(event.target.checked)}
                  />
                  <span>Clean CSV</span>
                </label>

                <button
                  type="button"
                  className="footer-icon-btn"
                  onClick={() => videoInputRef.current?.click()}
                  aria-label="Add demo video"
                  title="Add demo video"
                >
                  <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" focusable="false">
                    <path d="M4 6.5C4 5.67 4.67 5 5.5 5H14.5C15.33 5 16 5.67 16 6.5V9.5L20 7V17L16 14.5V17.5C16 18.33 15.33 19 14.5 19H5.5C4.67 19 4 18.33 4 17.5V6.5Z" fill="currentColor" />
                  </svg>
                </button>

                <button
                  type="button"
                  className="footer-icon-btn footer-icon-btn-muted"
                  aria-label="Emoji picker"
                  title="Emoji picker"
                  disabled
                >
                  <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false">
                    <path d="M12 22C6.48 22 2 17.52 2 12S6.48 2 12 2S22 6.48 22 12S17.52 22 12 22ZM8.5 10.5C9.33 10.5 10 9.83 10 9S9.33 7.5 8.5 7.5S7 8.17 7 9S7.67 10.5 8.5 10.5ZM15.5 10.5C16.33 10.5 17 9.83 17 9S16.33 7.5 15.5 7.5S14 8.17 14 9S14.67 10.5 15.5 10.5ZM12 18C14.5 18 16.65 16.64 17.82 14.62L16.09 13.62C15.27 15 13.76 16 12 16C10.24 16 8.73 15 7.91 13.62L6.18 14.62C7.35 16.64 9.5 18 12 18Z" fill="currentColor" />
                  </svg>
                </button>

                <button
                  type="button"
                  className="footer-icon-btn upload-icon-btn"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={uploadStatus === "uploading"}
                  title="Upload CSV files"
                >
                  {uploadStatus === "uploading" ? (
                    "..."
                  ) : (
                    <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" focusable="false">
                      <path
                        d="M12 4L8 8H10.6V14H13.4V8H16L12 4ZM6 17H18V20H6V17Z"
                        fill="currentColor"
                      />
                    </svg>
                  )}
                </button>
              </div>

              <button
                type="button"
                onClick={() => {
                  void handleSendMessage();
                }}
                disabled={chatStatus === "sending"}
                className="primary-chat-btn chat-send-toolbar-btn"
                aria-label="Send message"
              >
                {chatStatus === "sending" ? (
                  "..."
                ) : (
                  <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false">
                    <path d="M3.4 20.4L21 12L3.4 3.6L3.3 10.1L15.9 12L3.3 13.9L3.4 20.4Z" fill="currentColor" />
                  </svg>
                )}
              </button>
            </div>

            <input
              type="text"
              value={chatInput}
              onChange={(event) => setChatInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  void handleSendMessage();
                }
              }}
              placeholder="Type your message..."
              className="chat-input"
            />
          </div>
        </section>
      )}
    </main>
  );
}