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
          <div className="capability-table-scroll">
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

          <section className="format-definitions" aria-labelledby="format-definitions-title">
            <h3 id="format-definitions-title">名称说明</h3>
            <dl>
              <div>
                <dt>ProbHub Workspace</dt>
                <dd>可继续编辑的单题或多题工作区；根目录含 .probhub/workspace.yaml，每题使用 probhub.yaml。</dd>
              </div>
              <div>
                <dt>ProbHub Legacy</dt>
                <dd>旧版单题工程目录，通常由 meta.json、problem*.md、data/ 和出题源码组成。</dd>
              </div>
              <div>
                <dt>ProbHub Core</dt>
                <dd>ProbHub 生成的单题导出，采用 DOMjudge 兼容结构；与 ICPC legacy 题包可能具有相同目录特征。</dd>
              </div>
              <div>
                <dt>ICPC Problem Package</dt>
                <dd>跨评测系统的通用题包规范；本项目支持 legacy-icpc 与 2025-09 两种输出 profile。</dd>
              </div>
              <div>
                <dt>DOMjudge / Kattis</dt>
                <dd>可导入 ICPC 题包的评测平台与工具链，不是这里单独的一种源格式。</dd>
              </div>
            </dl>
          </section>
        </div>
      </div>
    </dialog>
  );
});
