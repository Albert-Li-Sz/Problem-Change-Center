import { Terminal } from "lucide-react";

export function LogViewer({ logs }: { logs: string }) {
  return (
    <section className="panel log-panel">
      <div className="panel-heading">
        <div>
          <h2>运行日志</h2>
          <p>stdout 和 stderr 会合并显示，便于定位转换失败原因。</p>
        </div>
        <Terminal size={20} aria-hidden="true" />
      </div>
      <pre className="log-viewer">{logs || "等待任务启动..."}</pre>
    </section>
  );
}
