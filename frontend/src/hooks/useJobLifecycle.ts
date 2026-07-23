import { Dispatch, MutableRefObject, SetStateAction, useEffect } from "react";

import {
  ConversionReport,
  getJob,
  getLogs,
  getReport,
  JobResponse,
  LogChunk,
  subscribeToJobEvents
} from "../api";


const pollIntervalMs = 1200;
const sseRetryDelaysMs = [1000, 2000, 5000, 10000];

export type LogChunkAction = "append" | "ignore" | "replace" | "resync";

export function logChunkAction(
  currentOffset: number,
  chunk: LogChunk
): LogChunkAction {
  if (!chunk.reset && chunk.next_offset <= currentOffset) return "ignore";
  if (chunk.reset || chunk.offset === 0) return "replace";
  if (chunk.offset !== currentOffset) return "resync";
  return "append";
}

type JobLifecycleOptions = {
  job: JobResponse | null;
  requestEpoch: MutableRefObject<number>;
  setJob: Dispatch<SetStateAction<JobResponse | null>>;
  setLogs: Dispatch<SetStateAction<string>>;
  setReport: Dispatch<SetStateAction<ConversionReport | null>>;
  setError: Dispatch<SetStateAction<string | null>>;
};


export function useJobLifecycle({
  job,
  requestEpoch,
  setJob,
  setLogs,
  setReport,
  setError
}: JobLifecycleOptions): void {
  useEffect(() => {
    if (!job || (job.status !== "queued" && job.status !== "running")) return;
    const jobId = job.id;
    const epoch = requestEpoch.current;
    let stopped = false;
    let terminal = false;
    let pollTimer: number | undefined;
    let reconnectTimer: number | undefined;
    let closeStream: (() => void) | null = null;
    let pollInFlight = false;
    let reconnectAttempts = 0;
    let reportPending = false;
    let reportReceived = false;
    let eventCursor = "";
    let logOffset = 0;

    const isCurrent = () => !stopped && requestEpoch.current === epoch;
    const stopPolling = () => {
      if (pollTimer !== undefined) window.clearTimeout(pollTimer);
      pollTimer = undefined;
    };
    const schedulePoll = (delay = pollIntervalMs) => {
      stopPolling();
      pollTimer = window.setTimeout(poll, delay);
    };
    const poll = async () => {
      if (!isCurrent() || (terminal && !reportPending) || pollInFlight) return;
      pollInFlight = true;
      try {
        const [nextJob, nextLogs] = await Promise.all([
          getJob(jobId),
          getLogs(jobId)
        ]);
        if (!isCurrent()) return;
        reportPending = nextJob.report_ready && !reportReceived;
        let nextReport: ConversionReport | null = null;
        if (reportPending) {
          try {
            nextReport = await getReport(jobId);
            reportReceived = true;
            reportPending = false;
          } catch (error) {
            if (isCurrent()) {
              setError(
                error instanceof Error ? error.message : "无法读取转换报告"
              );
            }
          }
        }
        if (!isCurrent()) return;
        terminal = !["queued", "running"].includes(nextJob.status);
        setJob(nextJob);
        setLogs(nextLogs);
        logOffset = new TextEncoder().encode(nextLogs).length;
        const revision = eventCursor.split(":", 1)[0] || "0";
        eventCursor = `${revision}:${logOffset}`;
        if (nextReport) setReport(nextReport);
        if (!terminal || reportPending) schedulePoll();
      } catch (error) {
        if (!isCurrent()) return;
        setError(error instanceof Error ? error.message : "无法读取任务状态");
        schedulePoll();
      } finally {
        pollInFlight = false;
      }
    };

    const connect = () => {
      if (!isCurrent() || terminal) return;
      closeStream?.();
      closeStream = subscribeToJobEvents(
        jobId,
        {
          onOpen: stopPolling,
          onJob: (nextJob) => {
            if (!isCurrent()) return;
            terminal = !["queued", "running"].includes(nextJob.status);
            reportPending = nextJob.report_ready && !reportReceived;
            setJob(nextJob);
          },
          onLogs: (chunk) => {
            if (!isCurrent()) return;
            const action = logChunkAction(logOffset, chunk);
            if (action === "ignore") return;
            if (action === "resync") {
              schedulePoll(0);
              return;
            }
            setLogs((current) =>
              action === "replace" ? chunk.text : current + chunk.text
            );
            logOffset = chunk.next_offset;
          },
          onReport: (nextReport) => {
            if (!isCurrent()) return;
            reportReceived = true;
            reportPending = false;
            setReport(nextReport);
          },
          onCursor: (cursor) => {
            eventCursor = cursor;
          },
          onError: () => {
            if (!isCurrent()) return;
            closeStream?.();
            closeStream = null;
            if (terminal) {
              if (reportPending) schedulePoll(0);
              return;
            }
            schedulePoll(0);
            if (reconnectAttempts < sseRetryDelaysMs.length) {
              const delay = sseRetryDelaysMs[reconnectAttempts];
              reconnectAttempts += 1;
              reconnectTimer = window.setTimeout(connect, delay);
            }
          }
        },
        eventCursor
      );
      if (closeStream === null) schedulePoll(0);
    };

    connect();

    return () => {
      stopped = true;
      stopPolling();
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
      closeStream?.();
    };
  }, [job?.id, requestEpoch, setError, setJob, setLogs, setReport]);
}
