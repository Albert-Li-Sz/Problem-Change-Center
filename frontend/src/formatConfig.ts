import { FormatId, InspectResult, JobResponse, TargetFormat } from "./api";


export const formatLabels: Record<FormatId, string> = {
  polygon: "Polygon / Codeforces",
  probhub: "ProbHub Workspace / Legacy / DOMjudge",
  hydro: "HydroOJ",
  icpc: "ICPC / DOMjudge / Kattis",
  hoj: "HOJ",
  fps: "FPS / HUSTOJ",
  qduoj: "QDUOJ",
  uoj: "UOJ",
  dmoj: "DMOJ / LQDOJ",
  generic: "通用测试数据目录"
};

export const readableFormats = Object.keys(formatLabels) as FormatId[];
export const writableFormats: TargetFormat[] = [
  "hydro",
  "icpc",
  "hoj",
  "fps",
  "qduoj",
  "uoj",
  "dmoj"
];

export const packageKinds: Record<FormatId, string> = {
  polygon: "单题源包或比赛包",
  probhub: "单题目录或多题工作区",
  hydro: "单题或多题完整包",
  icpc: "单题目录或嵌套多题包",
  hoj: "单题或多题完整包",
  fps: "单题或多题 XML",
  qduoj: "单题或多题完整包",
  uoj: "单题或多题评测数据 + sidecar",
  dmoj: "单题或多题评测数据 + sidecar",
  generic: "单题或多题推断目录"
};

export const packageScopeLabels: Record<InspectResult["package_scope"], string> = {
  single: "单题包",
  multi: "多题包",
  unknown: "题目数量未知"
};

export const packageLayoutLabels: Record<InspectResult["package_layout"], string> = {
  directory: "目录结构",
  contest: "比赛结构",
  workspace: "工作区结构",
  nested: "嵌套单题 ZIP",
  xml: "XML 集合",
  mixed: "混合结构",
  unknown: "未知结构"
};

export const progressLabels: Record<
  NonNullable<JobResponse["progress"]>["phase"],
  string
> = {
  validate_archive: "校验压缩包",
  extract: "安全解压",
  detect: "识别格式",
  read: "读取源题包",
  validate_ir: "校验题目语义",
  write: "写出目标题包",
  validate_output: "校验输出",
  package: "打包结果"
};

export const projectUrl =
  "https://github.com/LMTINSUZHOU/Problem-Change-Center";

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function formatElapsed(startedAt: string, lastActivityAt: string): string {
  const elapsed = Math.max(
    0,
    Math.floor(
      (new Date(lastActivityAt).getTime() - new Date(startedAt).getTime()) / 1000
    )
  );
  if (elapsed < 60) return `${elapsed} 秒`;
  const minutes = Math.floor(elapsed / 60);
  const seconds = elapsed % 60;
  return `${minutes} 分 ${seconds} 秒`;
}
