"use client";

import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { ChartSpec } from "@/types/api";

type ChatChartProps = {
  chartSpec: ChartSpec | null | undefined;
};

const PIE_COLORS = ["#2563eb", "#16a34a", "#f59e0b", "#dc2626", "#7c3aed", "#0891b2", "#84cc16"];

function coerceNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string") {
    const cleaned = value.replace(/,/g, "").replace(/%/g, "").trim();
    const numeric = Number(cleaned);
    if (Number.isFinite(numeric)) {
      return numeric;
    }
  }
  return null;
}

function toRows(chartSpec: ChartSpec): Array<Record<string, unknown>> {
  if (Array.isArray(chartSpec.series)) {
    return chartSpec.series;
  }
  if (Array.isArray(chartSpec.data)) {
    return chartSpec.data;
  }
  if (
    chartSpec.data &&
    typeof chartSpec.data === "object" &&
    Array.isArray(chartSpec.data.values)
  ) {
    return chartSpec.data.values;
  }
  return [];
}

function normalizeContract(chartSpec: ChartSpec): {
  chartType: "none" | "pie" | "donut" | "bar" | "line" | "area" | "scatter" | "kpi" | "combined";
  title: string;
  metadata: Record<string, unknown>;
} {
  const rawType = String(chartSpec.chart_type || chartSpec.type || "none").toLowerCase();
  const chartType =
    rawType === "pie" || rawType === "donut" || rawType === "bar" || rawType === "line" || rawType === "area" || rawType === "scatter" || rawType === "kpi" || rawType === "combined"
      ? rawType
      : "none";

  const metadata =
    chartSpec.metadata && typeof chartSpec.metadata === "object"
      ? (chartSpec.metadata as Record<string, unknown>)
      : {};

  const resolvedTitle = Array.isArray(chartSpec.title)
    ? chartSpec.title[0] || "Chart"
    : chartSpec.title || "Chart";

  return {
    chartType,
    title: resolvedTitle,
    metadata,
  };
}

function rowsFromContract(
  chartType: "none" | "pie" | "donut" | "bar" | "line" | "area" | "scatter" | "kpi" | "combined",
  metadata: Record<string, unknown>,
): Array<Record<string, unknown>> {
  if (chartType === "pie" || chartType === "donut") {
    const slices = metadata.slices;
    if (Array.isArray(slices)) {
      const rows = slices
        .map((slice) => {
          if (!slice || typeof slice !== "object") {
            return null;
          }
          const row = slice as Record<string, unknown>;
          const label = String(row.label ?? "");
          const value = coerceNumber(row.value);
          if (!label || value === null) {
            return null;
          }
          return { label, value };
        })
        .filter((row): row is { label: string; value: number } => row !== null);
      if (rows.length) {
        return rows;
      }
    }

    const labels = Array.isArray(metadata.labels) ? metadata.labels.map((item) => String(item)) : [];
    const data = Array.isArray(metadata.data) ? metadata.data.map((item) => coerceNumber(item)).filter((item): item is number => item !== null) : [];
    if (labels.length && data.length && labels.length === data.length) {
      return labels.map((label, index) => ({ label, value: data[index] }));
    }

    const keyValueRows = Object.entries(metadata)
      .map(([key, value]) => ({ label: key, value: coerceNumber(value) }))
      .filter((row) => row.value !== null)
      .map((row) => ({ label: row.label, value: row.value as number }));
    return keyValueRows;
  }

  if (chartType === "scatter") {
    const points = metadata.points;
    if (Array.isArray(points)) {
      const normalized = points
        .map((point) => {
          if (!point || typeof point !== "object") {
            return null;
          }
          const row = point as Record<string, unknown>;
          const x = coerceNumber(row.x);
          const y = coerceNumber(row.y);
          if (x === null || y === null) {
            return null;
          }
          return {
            x,
            y,
            label: row.label ? String(row.label) : undefined,
          };
        })
        .filter((row) => row !== null) as Array<{ x: number; y: number; label?: string }>;
      if (normalized.length) {
        return normalized;
      }
    }

    const xData = Array.isArray(metadata.x_data)
      ? metadata.x_data.map((item) => coerceNumber(item)).filter((item): item is number => item !== null)
      : [];
    const yData = Array.isArray(metadata.y_data)
      ? metadata.y_data.map((item) => coerceNumber(item)).filter((item): item is number => item !== null)
      : [];
    const pairCount = Math.min(xData.length, yData.length);
    if (pairCount > 0) {
      return Array.from({ length: pairCount }, (_, index) => ({ x: xData[index], y: yData[index] }));
    }
  }

  const labels = Array.isArray(metadata.labels) ? metadata.labels.map((item) => String(item)) : [];
  const data = Array.isArray(metadata.data) ? metadata.data.map((item) => coerceNumber(item)).filter((item): item is number => item !== null) : [];
  const xField = typeof metadata.x === "string" ? metadata.x : "x";
  const yField = typeof metadata.y === "string" ? metadata.y : "y";
  if (labels.length && data.length && labels.length === data.length) {
    return labels.map((label, index) => ({ [xField]: label, [yField]: data[index] }));
  }

  return [];
}

function applyFilter(
  rows: Array<Record<string, unknown>>,
  filter: Record<string, unknown> | undefined,
): Array<Record<string, unknown>> {
  if (!filter) {
    return rows;
  }

  const filterEntries = Object.entries(filter).filter(([key]) =>
    rows.some((row) => Object.prototype.hasOwnProperty.call(row, key)),
  );

  if (!filterEntries.length) {
    return rows;
  }

  return rows.filter((row) =>
    filterEntries.every(([key, value]) => String(row[key]) === String(value)),
  );
}

function normalizeKeyName(value: string): string {
  return value.replace(/[^a-z0-9]/gi, "").toLowerCase();
}

function resolveFieldName(
  rows: Array<Record<string, unknown>>,
  requestedField: string | undefined,
  fallbackKind: "x" | "y",
): string {
  const keys = Array.from(
    new Set(rows.flatMap((row) => Object.keys(row))),
  );

  if (!keys.length) {
    return fallbackKind === "x" ? "x" : "y";
  }

  const requested = (requestedField || "").trim();
  if (requested && keys.includes(requested)) {
    return requested;
  }

  if (requested) {
    const normalizedRequested = normalizeKeyName(requested);
    const normalizedMatch = keys.find(
      (key) => normalizeKeyName(key) === normalizedRequested,
    );
    if (normalizedMatch) {
      return normalizedMatch;
    }
  }

  const aliasMap: Record<string, string[]> = {
    count: ["count", "review_count", "reviews", "total", "n", "value", "cnt"],
    review_count: ["review_count", "count", "reviews", "total", "n", "value"],
    pct: ["pct", "percent", "percentage", "share", "ratio"],
    percent: ["percent", "percentage", "pct", "share", "ratio"],
    rating: ["rating", "stars", "score", "label", "category"],
  };

  if (requested) {
    const aliases = aliasMap[normalizeKeyName(requested)] || [];
    if (aliases.length) {
      const aliasMatch = keys.find((key) =>
        aliases.includes(normalizeKeyName(key)),
      );
      if (aliasMatch) {
        return aliasMatch;
      }
    }
  }

  const numericKeys = keys.filter((key) =>
    rows.some((row) => {
      const value = row[key];
      if (typeof value === "number") {
        return Number.isFinite(value);
      }
      if (typeof value === "string") {
        const numeric = Number(value.replace(/,/g, ""));
        return Number.isFinite(numeric);
      }
      return false;
    }),
  );

  if (fallbackKind === "y") {
    return numericKeys[0] || keys[0];
  }

  const categorical = keys.find((key) => !numericKeys.includes(key));
  return categorical || keys[0];
}

function coerceChartValue(value: unknown, axisType: "x" | "y"): string | number {
  if (axisType === "x") {
    return String(value ?? "");
  }
  if (typeof value === "number") {
    return value;
  }
  if (typeof value === "string") {
    const numeric = Number(value.replace(/,/g, ""));
    if (Number.isFinite(numeric)) {
      return numeric;
    }
  }
  return 0;
}

function formatMetricValue(
  value: number,
  format: string | undefined,
  decimalsFallback: number,
): string {
  const decimals = /\.([0-9])f/.exec(format || "")?.[1];
  const resolvedDecimals = decimals ? Number(decimals) : decimalsFallback;
  const hasCurrency = (format || "").includes("$");
  const formatted = value.toLocaleString(undefined, {
    minimumFractionDigits: resolvedDecimals,
    maximumFractionDigits: resolvedDecimals,
  });
  return hasCurrency ? `$${formatted}` : formatted;
}

function renderStandardChart(
  chartType: "line" | "bar" | "area",
  title: string,
  x: string,
  y: string,
  rows: Array<Record<string, unknown>>,
) {
  if (!rows.length) {
    return (
      <div
        style={{
          border: "1px dashed #d1d5db",
          borderRadius: "8px",
          padding: "0.75rem",
          color: "#6b7280",
          marginTop: "0.6rem",
        }}
      >
        Chart requested, but no rows are available for visualization.
      </div>
    );
  }

  return (
    <div style={{ marginTop: "0.75rem" }}>
      <p style={{ margin: "0 0 0.4rem 0", fontWeight: 600 }}>{title}</p>
      <div style={{ width: "100%", height: 280 }}>
        <ResponsiveContainer>
          {chartType === "line" ? (
            <LineChart data={rows} margin={{ top: 8, right: 8, bottom: 8, left: 8 }}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey={x} />
              <YAxis />
              <Tooltip />
              <Line type="monotone" dataKey={y} stroke="#2563eb" strokeWidth={2} dot={false} />
            </LineChart>
          ) : chartType === "area" ? (
            <AreaChart data={rows} margin={{ top: 8, right: 8, bottom: 8, left: 8 }}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey={x} />
              <YAxis />
              <Tooltip />
              <Area type="monotone" dataKey={y} stroke="#0ea5e9" fill="#bae6fd" strokeWidth={2} />
            </AreaChart>
          ) : (
            <BarChart data={rows} margin={{ top: 8, right: 8, bottom: 8, left: 8 }}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey={x} />
              <YAxis />
              <Tooltip />
              <Bar dataKey={y} fill="#059669" />
            </BarChart>
          )}
        </ResponsiveContainer>
      </div>
    </div>
  );
}

function renderScatterChart(
  title: string,
  rows: Array<Record<string, unknown>>,
  xLabel?: string,
  yLabel?: string,
) {
  if (!rows.length) {
    return (
      <div
        style={{
          border: "1px dashed #d1d5db",
          borderRadius: "8px",
          padding: "0.75rem",
          color: "#6b7280",
          marginTop: "0.6rem",
        }}
      >
        Chart requested, but no rows are available for visualization.
      </div>
    );
  }

  return (
    <div style={{ marginTop: "0.75rem" }}>
      <p style={{ margin: "0 0 0.4rem 0", fontWeight: 600 }}>{title}</p>
      <div style={{ width: "100%", height: 300 }}>
        <ResponsiveContainer>
          <ScatterChart margin={{ top: 8, right: 8, bottom: 8, left: 8 }}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis type="number" dataKey="x" name={xLabel || "x"} />
            <YAxis type="number" dataKey="y" name={yLabel || "y"} />
            <Tooltip cursor={{ strokeDasharray: "3 3" }} />
            <Scatter data={rows} fill="#f97316" />
          </ScatterChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

function SingleChart({ chartSpec }: ChatChartProps) {
  if (!chartSpec || typeof chartSpec !== "object") {
    return null;
  }

  const contract = normalizeContract(chartSpec);
  if (contract.chartType === "none") {
    return null;
  }

  if (contract.chartType === "scatter") {
    const scatterRows = rowsFromContract(contract.chartType, contract.metadata);
    if (scatterRows.length) {
      return renderScatterChart(
        contract.title,
        scatterRows,
        typeof contract.metadata.x === "string" ? contract.metadata.x : "x",
        typeof contract.metadata.y === "string" ? contract.metadata.y : "y",
      );
    }
  }

  if (contract.chartType === "pie" || contract.chartType === "donut") {
    const contractRows = rowsFromContract(contract.chartType, contract.metadata);
    const pieRows = contractRows.length ? contractRows : toRows(chartSpec);

    if (!pieRows.length) {
      return (
        <div
          style={{
            border: "1px dashed #d1d5db",
            borderRadius: "8px",
            padding: "0.75rem",
            color: "#6b7280",
            marginTop: "0.6rem",
          }}
        >
          Chart requested, but no rows are available for visualization.
        </div>
      );
    }

    return (
      <div style={{ marginTop: "0.75rem" }}>
        <p style={{ margin: "0 0 0.4rem 0", fontWeight: 600 }}>{contract.title}</p>
        <div style={{ width: "100%", height: 320 }}>
          <ResponsiveContainer>
            <PieChart>
              <Tooltip />
              <Legend />
              <Pie
                data={pieRows}
                dataKey="value"
                nameKey="label"
                cx="50%"
                cy="50%"
                outerRadius={100}
                innerRadius={contract.chartType === "donut" ? 55 : 0}
                label
              >
                {pieRows.map((_, index) => (
                  <Cell key={`pie-cell-${index}`} fill={PIE_COLORS[index % PIE_COLORS.length]} />
                ))}
              </Pie>
            </PieChart>
          </ResponsiveContainer>
        </div>
      </div>
    );
  }

  const rawChartType = chartSpec.chart_type || chartSpec.type || "bar";
  const chartType =
    rawChartType === "line"
      ? "line"
      : rawChartType === "area"
        ? "area"
        : rawChartType === "scatter"
          ? "scatter"
      : rawChartType === "kpi"
        ? "kpi"
        : rawChartType === "combined"
          ? "combined"
          : "bar";
  const title = Array.isArray(chartSpec.title)
    ? chartSpec.title[0] || "Chart"
    : chartSpec.title || "Chart";
  const x = chartSpec.x || "x";
  const y = chartSpec.y || "y";
  const series = toRows(chartSpec);

  if (chartType === "kpi") {
    const firstPoint = series[0] || {};
    const label = typeof firstPoint.label === "string" ? firstPoint.label : title;
    const rawValue = typeof firstPoint.value === "number" ? firstPoint.value : Number(firstPoint.value ?? 0);
    const decimals = typeof chartSpec.format?.decimals === "number" ? chartSpec.format.decimals : 2;

    let formattedValue = rawValue.toLocaleString(undefined, {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    });
    if (chartSpec.format?.type === "currency") {
      formattedValue = `${chartSpec.format.currency || ""}${formattedValue}`.trim();
    }

    return (
      <div
        style={{
          marginTop: "0.75rem",
          borderRadius: "14px",
          padding: "0.95rem",
          background: "linear-gradient(135deg, rgba(108, 92, 231, 0.12) 0%, rgba(237, 233, 254, 0.82) 100%)",
          border: "1px solid rgba(108, 92, 231, 0.16)",
        }}
      >
        <p style={{ margin: "0 0 0.3rem 0", fontSize: "0.78rem", fontWeight: 600, color: "#5b21b6", textTransform: "uppercase", letterSpacing: "0.08em" }}>
          {label}
        </p>
        <p style={{ margin: 0, fontSize: "1.7rem", fontWeight: 700, color: "#1f2937" }}>
          {formattedValue}
        </p>
        {chartSpec.description && (
          <p style={{ margin: "0.45rem 0 0", fontSize: "0.82rem", lineHeight: 1.45, color: "#6b7280" }}>
            {chartSpec.description}
          </p>
        )}
      </div>
    );
  }

  if (chartType === "combined" && Array.isArray(chartSpec.views) && chartSpec.views.length > 0) {
    return (
      <div style={{ marginTop: "0.75rem", display: "grid", gap: "0.85rem" }}>
        {chartSpec.views.map((view, index) => {
          const filteredRows = applyFilter(series, view.filter);
          if ((view.type === "text" || view.view === "metric") && view.encoding?.text?.field) {
            const field = view.encoding.text.field;
            const values = filteredRows
              .map((row) => Number(row[field]))
              .filter((value) => !Number.isNaN(value));
            const aggregate = view.encoding.text.aggregate || "max";
            const metricValue = values.length
              ? aggregate === "sum"
                ? values.reduce((total, value) => total + value, 0)
                : Math.max(...values)
              : 0;

            return (
              <div
                key={`${view.title || view.view || view.type || "metric"}-${index}`}
                style={{
                  borderRadius: "14px",
                  padding: "0.95rem",
                  background: "linear-gradient(135deg, rgba(108, 92, 231, 0.12) 0%, rgba(237, 233, 254, 0.82) 100%)",
                  border: "1px solid rgba(108, 92, 231, 0.16)",
                }}
              >
                <p style={{ margin: "0 0 0.3rem 0", fontSize: "0.78rem", fontWeight: 600, color: "#5b21b6", textTransform: "uppercase", letterSpacing: "0.08em" }}>
                  {view.title || title}
                </p>
                <p style={{ margin: 0, fontSize: "1.7rem", fontWeight: 700, color: "#1f2937" }}>
                  {formatMetricValue(metricValue, view.encoding.text.format, 2)}
                </p>
              </div>
            );
          }

          if ((view.type === "bar" || view.type === "line") && view.encoding?.x?.field && view.encoding?.y?.field) {
            const xField = resolveFieldName(filteredRows, view.encoding.x.field, "x");
            const yField = resolveFieldName(filteredRows, view.encoding.y.field, "y");
            const normalizedRows = filteredRows
              .filter((row) => row[xField] !== undefined && row[yField] !== undefined)
              .map((row) => ({
                ...row,
                [xField]: coerceChartValue(row[xField], "x"),
                [yField]: coerceChartValue(row[yField], "y"),
              }));

            return (
              <div key={`${view.title || view.view || view.type || "chart"}-${index}`}>
                {renderStandardChart(
                  view.type === "line" ? "line" : "bar",
                  view.title || title,
                  xField,
                  yField,
                  normalizedRows,
                )}
              </div>
            );
          }

          return null;
        })}
      </div>
    );
  }

  if (!series.length) {
    const contractRows = rowsFromContract(contract.chartType, contract.metadata);
    if (contractRows.length) {
      if (contract.chartType === "scatter") {
        return renderScatterChart(
          contract.title,
          contractRows,
          typeof contract.metadata.x === "string" ? contract.metadata.x : "x",
          typeof contract.metadata.y === "string" ? contract.metadata.y : "y",
        );
      }

      const xKey = (typeof contract.metadata.x === "string" && contract.metadata.x) || "x";
      const yKey = (typeof contract.metadata.y === "string" && contract.metadata.y) || "y";
      return renderStandardChart(
        contract.chartType === "line"
          ? "line"
          : contract.chartType === "area"
            ? "area"
            : "bar",
        contract.title,
        xKey,
        yKey,
        contractRows,
      );
    }

    return (
      <div
        style={{
          border: "1px dashed #d1d5db",
          borderRadius: "8px",
          padding: "0.75rem",
          color: "#6b7280",
          marginTop: "0.6rem",
        }}
      >
        Chart requested, but no rows are available for visualization.
      </div>
    );
  }

  if (chartType === "scatter") {
    return renderScatterChart(title, series, x, y);
  }

  return (
    renderStandardChart(chartType === "line" ? "line" : chartType === "area" ? "area" : "bar", title, x, y, series)
  );
}

function expandMultiChartSpecs(chartSpec: ChartSpec): ChartSpec[] {
  if (!Array.isArray(chartSpec.chart_type)) {
    return [chartSpec];
  }

  const types = chartSpec.chart_type;
  const titles = Array.isArray(chartSpec.title) ? chartSpec.title : [];
  const metadataList = Array.isArray(chartSpec.metadata) ? chartSpec.metadata : [];

  return types.map((chartType, index) => ({
    ...chartSpec,
    chart_type: chartType,
    title: titles[index] || `Chart ${index + 1}`,
    metadata: (metadataList[index] && typeof metadataList[index] === "object") ? metadataList[index] : {},
  }));
}

export default function ChatChart({ chartSpec }: ChatChartProps) {
  if (!chartSpec || typeof chartSpec !== "object") {
    return null;
  }

  const chartSpecs = expandMultiChartSpecs(chartSpec).filter(
    (item) => item.chart_type !== "none",
  );

  if (!chartSpecs.length) {
    return null;
  }

  if (chartSpecs.length === 1) {
    return <SingleChart chartSpec={chartSpecs[0]} />;
  }

  return (
    <div style={{ marginTop: "0.4rem", display: "grid", gap: "0.95rem" }}>
      {chartSpecs.map((spec, index) => (
        <div key={`${String(spec.title || "chart")}-${index}`}>
          <SingleChart chartSpec={spec} />
        </div>
      ))}
    </div>
  );
}
