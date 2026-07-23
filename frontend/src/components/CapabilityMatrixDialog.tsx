import { forwardRef } from "react";
import { X } from "lucide-react";

import { TargetFormat } from "../api";
import {
  formatLabels,
  packageKinds,
  readableFormats,
  writableFormats
} from "../formatConfig";


export const CapabilityMatrixDialog = forwardRef<
  HTMLDialogElement,
  { onClose: () => void }
>(function CapabilityMatrixDialog({ onClose }, ref) {
  return (
    <dialog
      ref={ref}
      className="capability-dialog"
      aria-labelledby="capability-dialog-title"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="capability-dialog-surface">
        <div className="capability-dialog-heading">
          <div>
            <h2 id="capability-dialog-title">格式能力矩阵</h2>
            <p>查看各类题包的读取、输出和包结构支持情况。</p>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="关闭格式能力矩阵"
            title="关闭"
            onClick={onClose}
          >
            <X size={20} aria-hidden="true" />
          </button>
        </div>
        <div className="capability-matrix-scroll">
          <table>
            <thead>
              <tr>
                <th>格式</th>
                <th>读取</th>
                <th>输出</th>
                <th>包类型</th>
              </tr>
            </thead>
            <tbody>
              {readableFormats.map((format) => (
                <tr key={format}>
                  <td>{formatLabels[format]}</td>
                  <td>支持</td>
                  <td>
                    {writableFormats.includes(format as TargetFormat)
                      ? "支持"
                      : "—"}
                  </td>
                  <td>{packageKinds[format]}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </dialog>
  );
});
