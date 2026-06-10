import { Terminal } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

export function LogViewer({ logs }: { logs: string }) {
  const viewerRef = useRef<HTMLPreElement>(null);
  const [lineLimit, setLineLimit] = useState(300);

  const logView = useMemo(() => {
    if (!logs) return { text: "等待任务启动...", shown: 0, total: 0 };

    const normalized = logs.endsWith("\n") ? logs.slice(0, -1) : logs;
    const lines = normalized.split(/\r?\n/);
    const visibleLines = lineLimit === 0 ? lines : lines.slice(-lineLimit);

    return {
      text: visibleLines.join("\n"),
      shown: visibleLines.length,
      total: lines.length
    };
  }, [lineLimit, logs]);

  useEffect(() => {
    const viewer = viewerRef.current;
    if (!viewer) return;
    viewer.scrollTop = viewer.scrollHeight;
  }, [logView.text]);

  return (
    <section className="panel log-panel">
      <div className="panel-heading">
        <div className="log-title">
          <Terminal size={20} aria-hidden="true" />
          <h2>运行日志</h2>
        </div>
        <div className="log-controls">
          <span>{logView.total ? `${logView.shown}/${logView.total} 行` : "0 行"}</span>
          <select
            className="log-line-select"
            value={lineLimit}
            onChange={(event) => setLineLimit(Number(event.target.value))}
            aria-label="日志显示行数"
          >
            <option value={100}>最新 100 行</option>
            <option value={300}>最新 300 行</option>
            <option value={1000}>最新 1000 行</option>
            <option value={0}>全部</option>
          </select>
        </div>
      </div>
      <pre ref={viewerRef} className="log-viewer">{logView.text}</pre>
    </section>
  );
}
