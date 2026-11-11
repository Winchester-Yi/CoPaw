import { useState, useEffect } from "react";
import { useAppMessage } from "../../../hooks/useAppMessage";
import api from "../../../api";
import type {
  CronBatchDispatchOptions,
  CronJobSpecOutput,
} from "../../../api/types";
import { useAgentStore } from "../../../stores/agentStore";

type CronJob = CronJobSpecOutput;

export function useCronJobs() {
  const { selectedAgent } = useAgentStore();
  const [jobs, setJobs] = useState<CronJob[]>([]);
  const [loading, setLoading] = useState(false);
  const { message } = useAppMessage();

  const fetchJobs = async () => {
    setLoading(true);
    try {
      const data = await api.listCronJobs(true);
      if (data) {
        setJobs(data as CronJob[]);
      }
    } catch (error) {
      console.error("Failed to load cron jobs", error);
      message.error("Failed to load Cron Jobs");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    let mounted = true;

    const loadJobs = async () => {
      await fetchJobs();
    };

    if (mounted) {
      loadJobs();
    }

    return () => {
      mounted = false;
    };
  }, [selectedAgent]);

  const createJob = async (values: CronJob) => {
    try {
      const created = await api.createCronJob(values);
      setJobs((prev) => [created as CronJob, ...prev]);
      message.success("Created successfully");
      return true;
    } catch (error) {
      console.error("Failed to create cron job", error);
      message.error("Failed to save");
      return false;
    }
  };

  const updateJob = async (jobId: string, values: CronJob) => {
    const original = jobs.find((j) => j.id === jobId);
    const optimisticUpdate = { ...original, ...values };
    setJobs((prev) => prev.map((j) => (j.id === jobId ? optimisticUpdate : j)));

    try {
      const updated = await api.replaceCronJob(jobId, values);
      setJobs((prev) =>
        prev.map((j) => (j.id === jobId ? (updated as CronJob) : j)),
      );
      message.success("Updated successfully");
      return true;
    } catch (error) {
      console.error("Failed to update cron job", error);
      if (original) {
        setJobs((prev) => prev.map((j) => (j.id === jobId ? original : j)));
      }
      message.error("Failed to save");
      return false;
    }
  };

  const deleteJob = async (jobId: string) => {
    const original = jobs.find((j) => j.id === jobId);
    setJobs((prev) => prev.filter((j) => j.id !== jobId));

    try {
      await api.deleteCronJob(jobId);
      message.success("Deleted successfully");
      return true;
    } catch (error) {
      console.error("Failed to delete cron job", error);
      if (original) {
        setJobs((prev) => [...prev, original]);
      }
      message.error("Failed to delete");
      return false;
    }
  };

  const toggleEnabled = async (job: CronJob) => {
    const updated = { ...job, enabled: !job.enabled };
    setJobs((prev) => prev.map((j) => (j.id === job.id ? updated : j)));

    try {
      const returned = await api.replaceCronJob(job.id, updated);
      setJobs((prev) =>
        prev.map((j) => (j.id === job.id ? (returned as CronJob) : j)),
      );
      message.success(`${updated.enabled ? "Enabled" : "Disabled"}`);
      return true;
    } catch (error) {
      console.error("Failed to toggle cron job", error);
      setJobs((prev) => prev.map((j) => (j.id === job.id ? job : j)));
      message.error("Operation failed");
      return false;
    }
  };

  const setBatchDispatch = async (
    job: CronJob,
    enabled: boolean,
    options: CronBatchDispatchOptions = {},
  ): Promise<CronJob | null> => {
    const batchEnabled = job.meta?.broadcast_dispatch_intents_enabled === true;
    if (batchEnabled === enabled && !enabled) {
      return job;
    }
    try {
      const returned = enabled
        ? await api.enableCronBatchDispatch(job.id, options)
        : await api.disableCronBatchDispatch(job.id);
      setJobs((prev) =>
        prev.map((j) => (j.id === job.id ? (returned as CronJob) : j)),
      );
      message.success(enabled ? "批调度已启动" : "批调度已关闭");
      return returned as CronJob;
    } catch (error) {
      console.error("Failed to toggle cron batch dispatch", error);
      message.error("批调度操作失败");
      return null;
    }
  };

  const executeNow = async (jobId: string) => {
    const original = jobs.find((j) => j.id === jobId);
    setJobs((prev) =>
      prev.map((job) =>
        job.id === jobId
          ? {
              ...job,
              state: {
                ...job.state,
                last_status: "running",
                last_error: null,
              },
              task: job.task
                ? {
                    ...job.task,
                    is_running: true,
                  }
                : job.task,
            }
          : job,
      ),
    );

    try {
      await api.triggerCronJob(jobId);
      message.success("Task triggered successfully");
      return true;
    } catch (error) {
      console.error("Failed to execute cron job", error);
      if (original) {
        setJobs((prev) => prev.map((job) => (job.id === jobId ? original : job)));
      }
      message.error("Failed to execute");
      return false;
    }
  };

  return {
    jobs,
    loading,
    fetchJobs,
    createJob,
    updateJob,
    deleteJob,
    toggleEnabled,
    setBatchDispatch,
    executeNow,
  };
}
