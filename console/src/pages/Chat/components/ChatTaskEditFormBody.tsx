import { Checkbox, Form, Input, Select } from "@agentscope-ai/design";
import { TimePicker } from "antd";
import { useTranslation } from "react-i18next";
import styles from "../../Control/CronJobs/index.module.less";

export default function ChatTaskEditFormBody() {
  const { t } = useTranslation();

  return (
    <>
      <Form.Item
        name="name"
        label="任务名称"
        rules={[{ required: true, message: t("cronJobs.pleaseInputName") }]}
      >
        <Input placeholder={t("cronJobs.jobNamePlaceholder")} />
      </Form.Item>

      <Form.Item label="任务执行时间" required>
        <Form.Item name="cronType" noStyle>
          <Select>
            <Select.Option value="hourly">
              {t("cronJobs.cronTypeHourly")}
            </Select.Option>
            <Select.Option value="daily">
              {t("cronJobs.cronTypeDaily")}
            </Select.Option>
            <Select.Option value="weekly">
              {t("cronJobs.cronTypeWeekly")}
            </Select.Option>
            <Select.Option value="custom">
              {t("cronJobs.cronTypeCustom")}
            </Select.Option>
          </Select>
        </Form.Item>
      </Form.Item>

      <Form.Item
        noStyle
        shouldUpdate={(prev, cur) => prev.cronType !== cur.cronType}
      >
        {({ getFieldValue }) => {
          const cronType = getFieldValue("cronType");

          if (cronType === "daily" || cronType === "weekly") {
            return (
              <Form.Item
                name="cronTime"
                label={t("cronJobs.cronTime")}
                rules={[{ required: true }]}
              >
                <TimePicker
                  format="HH:mm"
                  minuteStep={15}
                  needConfirm={false}
                  style={{ width: "100%" }}
                />
              </Form.Item>
            );
          }
          return null;
        }}
      </Form.Item>

      <Form.Item
        noStyle
        shouldUpdate={(prev, cur) => prev.cronType !== cur.cronType}
      >
        {({ getFieldValue }) => {
          const cronType = getFieldValue("cronType");

          if (cronType === "weekly") {
            return (
              <Form.Item
                name="cronDaysOfWeek"
                label={t("cronJobs.cronDaysOfWeek")}
                rules={[{ required: true, message: "请选择至少一天" }]}
              >
                <Checkbox.Group
                  options={[
                    { label: t("cronJobs.cronDayMon"), value: "mon" },
                    { label: t("cronJobs.cronDayTue"), value: "tue" },
                    { label: t("cronJobs.cronDayWed"), value: "wed" },
                    { label: t("cronJobs.cronDayThu"), value: "thu" },
                    { label: t("cronJobs.cronDayFri"), value: "fri" },
                    { label: t("cronJobs.cronDaySat"), value: "sat" },
                    { label: t("cronJobs.cronDaySun"), value: "sun" },
                  ]}
                />
              </Form.Item>
            );
          }
          return null;
        }}
      </Form.Item>

      <Form.Item
        noStyle
        shouldUpdate={(prev, cur) => prev.cronType !== cur.cronType}
      >
        {({ getFieldValue }) => {
          const cronType = getFieldValue("cronType");

          if (cronType === "custom") {
            return (
              <Form.Item
                name="cronCustom"
                label="Cron 表达式"
                rules={[
                  { required: true, message: t("cronJobs.pleaseInputCron") },
                ]}
                extra={
                  <span className={styles.formExtraText}>
                    {t("cronJobs.cronExample")}
                  </span>
                }
              >
                <Input placeholder="0 9 * * *" />
              </Form.Item>
            );
          }
          return null;
        }}
      </Form.Item>

      <Form.Item
        noStyle
        shouldUpdate={(prev, cur) => prev.task_type !== cur.task_type}
      >
        {({ getFieldValue }) => {
          const taskType = getFieldValue("task_type");
          if (taskType === "workflow") return null;
          const isTextTask = taskType === "text";

          return (
            <Form.Item
              name="taskContentText"
              label={isTextTask ? "消息内容" : "请求内容"}
              required
              rules={[
                {
                  required: true,
                  message: isTextTask ? "请输入消息内容" : "请输入请求内容",
                },
              ]}
            >
              <Input.TextArea
                rows={6}
                placeholder={
                  isTextTask
                    ? "例如：今天 18 点提醒我整理日报"
                    : "例如：查询成都今天的天气"
                }
              />
            </Form.Item>
          );
        }}
      </Form.Item>
    </>
  );
}
